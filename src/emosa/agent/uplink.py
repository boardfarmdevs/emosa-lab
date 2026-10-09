# SPDX-License-Identifier: Apache-2.0
"""The agent performs the option-1 uplink switch itself (data-plane.md §5.3).

One ``UplinkSwitch`` per agent, with its own operation journal: the uplink is a
different scope from the fronthaul BSS, with its own lifecycle rules.

- **Credentials** come from the controller: the backhaul BSS of the applied M2
  set (multi-BSS mode). They can instead come from the agent configuration
  (``uplink.ssid`` and ``uplink.secret_ref``, a file in the agent's secret
  directory).
- **Upstream:** the BSSID the station may join (``uplink.bssid``), whatever
  the credential source. The station never picks a BSS by SSID alone: a pod
  whose M2 set includes the backhaul BSS serves that SSID itself, and a station
  on its own backhaul BSS loops ``br-home`` (data-plane.md §5.6).
- **When:** the pod is bound, reports a working uplink, serves the controller's
  fronthaul with no fronthaul operation in flight (the switch moves the path a
  fronthaul write travels on), and is not already on the controller's backhaul.
  One operation per OpenSync start of the pod (its radio rows, see
  ``UplinkBackend``), so the switch is re-applied after every re-onboarding.
  A pod restarts to its bootstrap uplink, option 2, whenever OpenSync restarts.
- **Applied** only when the pod's State shows the Multi-AP backhaul STA as
  ``cm``'s only uplink, on the same instance the switch was written to.
- **Held:** a switch that is not confirmed within the deadline (the pod has
  restarted to option 2, or will), or is rejected, or is changed by another
  manager, holds the pod on option 2. A failed switch's hold is bounded: after
  a backoff (``hold_backoff``, 10 minutes, doubling with each held retry to
  ``hold_backoff_cap``, 160 minutes) the switch is made again, once, and an
  applied one clears the hold; another manager's change holds until a new
  admission. A new admission clears any hold, because the fleet archives the
  agent's state.
- **Moved** on the controller's Backhaul Steering Request (``steer``): the station
  is pinned to the target BSS instead, by the same switch, while the pod is on
  its EasyMesh backhaul. A target on another band moves the uplink to the pod's
  backhaul station on that band, and the switch disables the other; a band the
  pod has no station on takes no move. The target is kept (``target.json``) for every later
  start, as long as the configured upstream is the one it replaced. A move that
  is not confirmed returns to the previous upstream instead of holding the pod:
  the controller asked for it, and hears of the failure. On a later start, a switch
  to the kept target that fails (its BSS gone, out of reach) drops it and switches
  to the configured upstream instead, once the pod is back on its bootstrap uplink
  (``cm`` reverts the station on the same start, or OpenSync restarts); only that
  switch holds.
"""

import json
import logging
import time

from emosa.errors import EmosaError, Reason
from emosa.model import ACTIVE, State
from emosa.opensync.uplink import MULTI_AP, UplinkIntent
from emosa.operations import transition
from emosa.reconcile import Engine

log = logging.getLogger("emosa.agent.uplink")
DEADLINE = 90  # observed: adopted in 27 s; OpenSync restarts a failed uplink in 40-120 s
SOURCE = "uplink-policy"
# a failed switch's hold: the first wait before the switch is made again, doubled with each
# held retry up to the cap (spec 8.3); seconds of wall time, kept in the hold's record
HOLD_BACKOFF, HOLD_BACKOFF_CAP = 600, 9600
OWNERSHIP_HOLD = "the station's configuration was changed by another manager"


class UplinkSwitch:
    def __init__(
        self,
        pod_id,
        backend,
        store,
        vault,
        credentials,
        *,
        bssid,
        run_id,
        settled=None,
        clock=None,
        hold_backoff=HOLD_BACKOFF,
        hold_backoff_cap=HOLD_BACKOFF_CAP,
        wall=None,
    ):
        """``credentials()`` -> (ssid, secret_ref) of the EasyMesh backhaul, or None.

        ``bssid`` is the upstream backhaul BSS the station is pinned to.
        ``settled()`` is true while the fronthaul scope is served and idle.
        ``wall()``: seconds of wall time, for a hold's backoff (default time.time).
        """
        self.hold_backoff, self.hold_backoff_cap = hold_backoff, hold_backoff_cap
        self.wall = wall or time.time
        self.pod_id, self.backend, self.store = pod_id, backend, store
        self.credentials, self.bssid, self.run_id = credentials, bssid, run_id
        self.settled = settled or (lambda: True)
        self.engine = Engine(store, vault, {pod_id: backend}, clock, intent_type=UplinkIntent)
        self.engine.recover()
        self.waiting = None  # why no switch is being made, for status
        # The controller's Backhaul Steering: the upstream it chose, over the configured one,
        # and the pod's backhaul station on that upstream's band (spec 8.3).
        self.configured = bssid
        self.configured_station = self.station = backend.station
        self.moves = 0  # each move is a switch of its own, even back to an earlier upstream
        # {"target", "previous", "station", "previous_station", "result"} of the latest move
        self.move = None
        self.target_path = store.directory / "target.json"
        # a held switch being made again: its hold's attempt (spec 8.3)
        self.hold_path = store.directory / "hold.json"
        # after a kept target failed: that switch's start and intent, until the pod is off it
        self.fallback_after = None
        try:
            kept = json.loads(self.target_path.read_text())
            if kept.get("configured") == bssid and kept.get("target"):
                self.bssid, self.moves = kept["target"], int(kept.get("moves", 1))
                if kept.get("station") in backend.bound:
                    self.station = kept["station"]
        except (OSError, ValueError, AttributeError):
            pass

    def latest(self):
        ops = self.store.operations()
        return ops[-1] if ops else None

    def held(self):
        return self.store.ownership(self.pod_id)

    def hold(self, op, reason):
        """Hold the pod on option 2. A failed switch's hold is bounded: the switch is made
        again after a backoff that doubles with each held retry (spec 8.3)."""
        if self.held():
            return
        evidence = {"operation_id": op.operation_id if op else None, "reason": reason}
        if reason != OWNERSHIP_HOLD:
            attempt = (self._retry() or 0) + 1
            wait = min(self.hold_backoff * 2 ** (attempt - 1), self.hold_backoff_cap)
            now = self.wall()
            evidence.update(held_at=now, attempt=attempt, retry_at=now + wait)
            log.warning(
                "uplink held on option 2: %s; switching again in %d s (hold %d)",
                reason,
                wait,
                attempt,
            )
        else:
            log.warning("uplink held on option 2: %s", reason)
        self.store.conflict(self.pod_id, evidence)

    def _hold_over(self):
        """A bounded hold's backoff is over: the hold released, its attempt kept in hold.json
        for the next hold's backoff and the retry's switch."""
        record = self.held()
        if record is None or record.get("reason") == OWNERSHIP_HOLD:
            return record is None
        if "retry_at" not in record:
            # held before holds were bounded: its backoff starts now
            now = self.wall()
            record.update(held_at=now, attempt=1, retry_at=now + self.hold_backoff)
            self.store.conflict(self.pod_id, record)
            return False
        if self.wall() < float(record["retry_at"]):
            return False
        self.hold_path.write_text(json.dumps({"attempt": int(record.get("attempt", 1))}) + "\n")
        self.store.release(self.pod_id)
        log.info("uplink hold %s over: switching again", record.get("attempt"))
        return True

    def _retry(self):
        """The attempt of the hold whose switch is being made again, or None."""
        try:
            return int(json.loads(self.hold_path.read_text())["attempt"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save(self, op, payload=None):
        self.engine._save(op, payload)

    def _settle(self, op, snap):
        """Confirm or time out the latest operation; hold on failure."""
        if op.state in (State.CONFIG_COMMITTED, State.INDETERMINATE):
            target = UplinkIntent(**op.intent).target(self.engine.vault)
            same_start = (
                snap is not None and snap.ready and self.backend.instance == op.plan.get("instance")
            )
            if (
                same_start
                and snap.observed.fresh
                and snap.observed.satisfies(target)
                and self.engine._matches(snap.config, target)
            ):
                evidence = self.engine._evidence(snap, instance=self.backend.instance)
                if op.state == State.INDETERMINATE:
                    evidence["attribution"] = "current_condition_only"
                transition(op, State.OBSERVED_APPLIED, evidence=evidence)
                op.application_evidence, op.reason = evidence, None
                self._save(op, {"observation": self.backend.facts})
                log.info("uplink on the EasyMesh backhaul: %s", self.backend.facts)
                if self._retry() is not None:
                    self.hold_path.unlink(missing_ok=True)
                    log.info("uplink switch applied on its retry: the hold's backoff cleared")
            elif self.engine._expired(op):
                op.original_outcome, op.deadline_elapsed = op.state, True
                transition(op, State.TIMED_OUT)
                op.reason = Reason.APPLY_TIMEOUT
                self._save(op, {"observation": self.backend.facts})
                reason = "not confirmed within the deadline"
                if not self._move_failed(op, reason) and not self._kept_failed(op, reason):
                    self.hold(op, "switch not confirmed within the deadline")
        elif op.state == State.OBSERVED_APPLIED and snap and snap.ready and snap.observed.fresh:
            target = UplinkIntent(**op.intent).target(self.engine.vault)
            same_start = self.backend.instance == op.plan.get("instance")
            if same_start and not self.engine._matches(snap.config, target):
                op.reason = Reason.OWNERSHIP_CONFLICT
                self._save(op, {"observation": self.backend.facts})
                self.hold(op, OWNERSHIP_HOLD)
        elif op.state in (State.REJECTED, State.FAILED, State.OWNERSHIP_CONFLICT):
            # Only a rejection before anything was sent may be retried, on a later start.
            if self._move_failed(op, f"{op.state.lower()}: {op.reason}"):
                return
            retryable = op.state == State.REJECTED and op.reason in (Reason.NOT_READY, Reason.BUSY)
            if not retryable and not self._kept_failed(op, f"{op.state.lower()}: {op.reason}"):
                self.hold(op, f"switch {op.state.lower()}: {op.reason}")

    def _keep_target(self):
        if self.bssid == self.configured and self.station == self.configured_station:
            self.target_path.unlink(missing_ok=True)
        else:
            self.target_path.write_text(
                json.dumps(
                    {
                        "configured": self.configured,
                        "target": self.bssid,
                        "station": self.station,
                        "moves": self.moves,
                    }
                )
            )

    def _move_failed(self, op, reason):
        """A move's switch failed: back to the previous upstream, and the station on it, no
        hold. False otherwise."""
        move = self.move
        if move is None or move["result"] is not None or op.intent.get("bssid") != move["target"]:
            return False
        log.warning(
            "backhaul move to %s failed (%s): back to %s", move["target"], reason, move["previous"]
        )
        move["result"] = reason
        self.bssid, self.station = move["previous"], move["previous_station"]
        self.moves += 1
        self._keep_target()
        return True

    def _kept_failed(self, op, reason):
        """A switch to the kept target failed on a later start: back to the configured
        upstream on this start, no hold. False otherwise (a switch to the configured
        upstream holds). Kept, the target held the pod on option 2 for good once its BSS
        was gone, and a held pod takes no Backhaul Steering either (rdk-1004, 5 Oct 2026)."""
        bssid = op.intent.get("bssid")
        if bssid == self.configured or bssid != self.bssid:
            return False
        log.warning(
            "kept backhaul target %s not reached (%s): back to the configured %s",
            bssid,
            reason,
            self.configured,
        )
        self.bssid, self.station = self.configured, self.configured_station
        self.moves += 1  # a switch of its own
        self._keep_target()
        self.fallback_after = {"instance": op.plan.get("instance"), "intent": op.intent}
        return True

    def steer(self, bssid, band=None, channel=None):
        """The controller's Backhaul Steering Request: move the uplink to ``bssid``.

        ``band`` is the target's ("2.4G", "5G", "6G", from its operating class): the move
        uses the pod's backhaul station on that band, which may be another one than the
        station in use (spec 8.3). A station on the radio that carries the pod's BSSes stays
        on that radio's channel: a target on another ``channel`` is refused. Returns None once
        the move is under way, or why it cannot be made now: "no_station_on_band" when the pod
        has no backhaul station on ``band``, "channel_not_operable" for such a channel.
        """
        bssid = bssid.lower()
        if self.held():
            return "held_on_option_2"
        if (self.backend.facts or {}).get("kind") != MULTI_AP:
            return "not_on_easymesh_backhaul"
        op = self.latest()
        if op is not None and op.state in ACTIVE:
            return "switch_in_progress"
        if bssid in {(self.backend.facts or {}).get("mac")}:
            return "own_station"
        station = self.station if band is None else self.backend.station_for(band)
        if station is None:
            return "no_station_on_band"
        fixed = self.backend.fixed_channel(station)
        if fixed is not None and channel is not None and channel != fixed:
            return "channel_not_operable"
        previous, previous_station = self.bssid, self.station
        self.move = {
            "target": bssid,
            "previous": previous,
            "station": station,
            "previous_station": previous_station,
            "result": None,
        }
        if (bssid, station) != (previous, previous_station):
            self.bssid, self.station = bssid, station
            self.moves += 1
            self._keep_target()
        log.info(
            "backhaul move requested: %s (%s) -> %s (%s)",
            previous,
            previous_station,
            bssid,
            station,
        )
        return None

    def steering_outcome(self, bssid):
        """None while the move to ``bssid`` is under way, True when applied, else why not."""
        move = self.move
        if move is None or move["target"] != bssid.lower():
            return "no_such_move"
        if move["result"] is not None:
            return move["result"]
        op = self.latest()
        facts = self.backend.facts or {}
        if (
            op is not None
            and op.state == State.OBSERVED_APPLIED
            and op.intent.get("bssid") == move["target"]
            and op.intent.get("station") == move["station"]
            and facts.get("kind") == MULTI_AP
            and facts.get("station") == move["station"]
            and facts.get("parent") == move["target"]
        ):
            move["result"] = True
            return True
        return None

    def _wanted(self, op, snap):
        """The intent to request now, or None (``self.waiting`` says why)."""
        if self.held() and not self._hold_over():
            self.waiting = "held on option 2"
            return None
        if not snap.ready:
            self.waiting = "pod not bound"
            return None
        credentials = self.credentials()
        if credentials is None:
            self.waiting = "no EasyMesh backhaul credentials"
            return None
        intent = UplinkIntent(self.pod_id, self.station, *credentials, bssid=self.bssid)
        try:
            intent.target(self.engine.vault)
        except EmosaError as exc:
            self.waiting = f"backhaul credentials unusable: {exc}"
            return None
        if op is not None and op.state in ACTIVE:
            self.waiting = "switch in progress"
            return None
        if op is not None and op.plan and op.plan.get("instance") == self.backend.instance:
            if op.state == State.OBSERVED_APPLIED and op.intent == intent.record():
                self.waiting = None  # applied on this start
                return None
            if op.state in (State.REJECTED, State.CANCELLED) and op.intent == intent.record():
                self.waiting = f"not switched on this start: {op.reason}"
                return None
        if (self.backend.facts or {}).get("kind") is None:
            self.waiting = "no working uplink yet"
            return None
        if self.fallback_after is not None:
            # A switch written while cm still acts on the failed one is reverted with it
            # (rdk-1004, 5 Oct 2026): first the station off the failed credential, or a new
            # start. Bounded: OpenSync restarts a pod left without a working uplink.
            failed = self.fallback_after
            if failed["instance"] == self.backend.instance and self.engine._matches(
                snap.config, UplinkIntent(**failed["intent"]).target(self.engine.vault)
            ):
                self.waiting = "the pod returning to its bootstrap uplink"
                return None
            self.fallback_after = None
        if not self.settled():
            self.waiting = "fronthaul not settled"
            return None
        self.waiting = None
        return intent

    async def tick(self):
        """One step, on the agent's refresh cadence."""
        try:
            snap = await self.backend.snapshot()
        except (EmosaError, ConnectionError, TimeoutError):
            # Not read since this process started. A switch in flight still times
            # out: the pod may be gone because of it (e.g. after an agent restart).
            snap = None
        op = self.latest()
        if op is not None:
            self._settle(op, snap)
        if snap is None:
            return
        intent = self._wanted(self.latest(), snap)
        if intent is None:
            return
        vault = self.engine.vault
        wanted = vault.fingerprint({**intent.record(), "target": intent.target(vault)})
        # one switch per start and credential, and per move of the controller's
        key = f"{self.backend.instance}:{wanted[:16]}" + (f":{self.moves}" if self.moves else "")
        retry = self._retry()
        if retry is not None:  # a held switch made again: one of its own, on the same start too
            key += f":retry{retry}"
        op = self.engine.request(
            intent, source=SOURCE, key=key, run_id=self.run_id, deadline=DEADLINE
        )
        if op.state != State.REQUESTED:
            return
        log.info("switching %s to the EasyMesh backhaul %r", intent.station, intent.ssid)
        op = await self.engine.execute(op.operation_id)
        log.info("uplink switch %s: %s %s", op.operation_id, op.state, op.reason or "")

    def status(self):
        op = self.latest()
        facts = self.backend.facts or {}
        return {
            "station": self.station,
            "active_station": self.backend.active,
            "bssid": self.bssid,
            "configured_bssid": self.configured,
            "move": self.move,
            "uplink": facts.get("kind"),
            "in_use": facts.get("in_use"),
            "parent": facts.get("parent"),
            "option": 1 if facts.get("kind") == MULTI_AP else 2,
            "held": self.held(),
            "waiting": self.waiting,
            "operation": None
            if op is None
            else {
                "operation_id": op.operation_id,
                "state": op.state,
                "reason": op.reason,
                "ssid": op.intent.get("ssid"),
                "instance": (op.plan or {}).get("instance"),
            },
        }


def m2_backhaul(store):
    """The backhaul BSS (SSID, secret reference) of the fronthaul scope's applied M2 set."""
    for op in reversed(store.operations()):
        if op.state == State.OBSERVED_APPLIED:
            for bss in op.intent.get("additional") or ():
                if bss.get("role") == "backhaul":
                    return bss["ssid"], bss["secret_ref"]
            return None
    return None
