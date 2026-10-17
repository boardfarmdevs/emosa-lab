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
  switch holds. A GRE child has no configured upstream: after a failed switch to its kept
  target, or a failed move from its bootstrap, it undoes its own write as the revert does,
  while the station still carries the failed switch's credential on that start (a pod whose
  fallback station keeps its uplink working never restarts to drop it).
- **Make-before-break:** a GRE move onto another station takes the station cm used, and any
  other EMOSA wrote, off once its own station joined the target with its gretap up: cm moves
  among working uplinks only at its periodic pass. A failed one undoes its write on that station
  first. A GRE child's record follows the station cm uses (spec 8.3).
"""

import json
import logging
import time

from emosa.errors import EmosaError, Reason
from emosa.model import ACTIVE, State
from emosa.opensync.uplink import BOOTSTRAP, GRE, MULTI_AP, UplinkIntent
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
        parents=None,
        hold_backoff=HOLD_BACKOFF,
        hold_backoff_cap=HOLD_BACKOFF_CAP,
        wall=None,
    ):
        """``credentials()`` -> (ssid, secret_ref) of the EasyMesh backhaul, or None.

        ``bssid`` is the upstream backhaul BSS the station is pinned to.
        ``settled()`` is true while the fronthaul scope is served and idle.
        ``parents()`` -> the BSSIDs that are GRE parent APs of this fleet (spec 8.6): a
        Backhaul Steering Request to one of them is a 3-address GRE move, not Multi-AP.
        ``wall()``: seconds of wall time, for a hold's backoff (default time.time).
        """
        self.hold_backoff, self.hold_backoff_cap = hold_backoff, hold_backoff_cap
        self.wall = wall or time.time
        self.pod_id, self.backend, self.store = pod_id, backend, store
        self.credentials, self.bssid, self.run_id = credentials, bssid, run_id
        self.settled = settled or (lambda: True)
        self.parents = parents or (lambda: frozenset())
        self.engine = Engine(store, vault, {pod_id: backend}, clock, intent_type=UplinkIntent)
        self.engine.recover()
        self.waiting = None  # why no switch is being made, for status
        # The controller's Backhaul Steering: the upstream it chose, over the configured one,
        # and the pod's backhaul station on that upstream's band (spec 8.3).
        self.configured = bssid
        self.configured_station = self.station = backend.station
        self.mode = MULTI_AP  # the current uplink's kind: a move to a GRE parent makes it GRE
        self.moves = 0  # each move is a switch of its own, even back to an earlier upstream
        # {"target", "previous", "station", "previous_station", "mode", "result"} of the latest move
        self.move = None
        self.target_path = store.directory / "target.json"
        # spec 8.6: the BSS the station was on before any EMOSA move (its bootstrap upstream,
        # cm's own association to the onboarding/GTP BSS), so a steer back to it is a revert.
        self.bootstrap_path = store.directory / "bootstrap.json"
        self.bootstrap = None  # {"bssid", "ssid"} once observed, persisted across restarts
        try:
            saved = json.loads(self.bootstrap_path.read_text())
            if isinstance(saved, dict) and saved.get("bssid"):
                self.bootstrap = {"bssid": saved["bssid"].lower(), "ssid": saved.get("ssid")}
        except (OSError, ValueError, AttributeError):
            pass
        # a held switch being made again: its hold's attempt (spec 8.3)
        self.hold_path = store.directory / "hold.json"
        # after a kept target failed: that switch's start and intent, until the pod is off it
        self.fallback_after = None
        self._instance = None  # the pod's start at the last read (_new_start)
        self._taken_off = set()  # the GRE switches whose other stations were taken off
        self._written = None  # the switch this process wrote last (its operation_id)
        try:
            kept = json.loads(self.target_path.read_text())
            if kept.get("configured") == bssid and kept.get("target"):
                self.bssid, self.moves = kept["target"], int(kept.get("moves", 1))
                self.mode = GRE if kept.get("mode") == GRE else MULTI_AP
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

    def _move_snapshot(self, station=None):
        """The vconf snapshot taken at the move from the bootstrap (the most recent operation that
        captured one, for ``station`` when given: each station has its own), for the revert to
        restore; None if there is none to undo."""
        for past in reversed(self.store.operations()):
            restore = (past.commit_evidence or {}).get("restore")
            if restore and (station is None or past.intent.get("station") == station):
                return restore
        return None

    def _settle_revert(self, op, snap):
        """A revert (spec 8.6): applied when the station is back on the bootstrap BSS in vconf mode
        (EMOSA's credential-list move gone). Confirmed by observation, not a credential fingerprint
        EMOSA cannot know. On a timeout the move's credential may linger; the pod returns to its pin
        on its next OpenSync restart, and EMOSA gives the revert up rather than loop."""
        if op.state not in (State.CONFIG_COMMITTED, State.INDETERMINATE):
            return
        facts = self.backend.facts or {}
        same_start = (
            snap is not None and snap.ready and self.backend.instance == op.plan.get("instance")
        )
        # the controller's steer back is confirmed with the station on the bootstrap BSS; the undo
        # of a failed switch (_undo) once EMOSA's credential is off the station, whichever uplink
        # cm then uses (the pod's fallback station may keep it)
        asked = (
            self.move is not None
            and self.move.get("mode") == BOOTSTRAP
            and self.move.get("result") is None
        )
        # the undo of a failed move onto another station than the uplink's (spec 8.3): that
        # station's own configuration tells, and the uplink stays on its own station and record
        undone = op.intent.get("station")
        other = not asked and undone != self.station
        config = self.backend.configs.get(undone, snap.config) if snap is not None else {}
        back = (
            same_start
            and snap.observed.fresh
            and self.bootstrap is not None
            and (
                not asked or (facts.get("kind") and facts.get("parent") == self.bootstrap["bssid"])
            )
            and config.get("uplink") == "other"  # the move's credential-list is gone (vconf)
        )
        if back and other:
            evidence = self.engine._evidence(snap, instance=self.backend.instance)
            transition(op, State.OBSERVED_APPLIED, evidence=evidence)
            op.application_evidence, op.reason = evidence, None
            self._save(op, {"observation": self.backend.facts})
            self.fallback_after = None
            log.info("the failed move's credential is off %s, back as the pod had it", undone)
        elif back:
            evidence = self.engine._evidence(snap, instance=self.backend.instance)
            transition(op, State.OBSERVED_APPLIED, evidence=evidence)
            op.application_evidence, op.reason = evidence, None
            self._save(op, {"observation": self.backend.facts})
            if self.move is not None and self.move.get("target") == self.bootstrap["bssid"]:
                self.move["result"] = True
            # the move is undone: back to no EMOSA pin, so nothing more is written
            self.mode = MULTI_AP
            self.bssid, self.station = self.configured, self.configured_station
            self.moves += 1
            self._keep_target()
            if asked:
                log.info(
                    "backhaul revert applied: the station is back on the bootstrap %s",
                    self.bootstrap["bssid"],
                )
            else:
                self.fallback_after = None
                log.info("the failed switch's credential is off %s", op.intent.get("station"))
        elif self.engine._expired(op) and other:
            op.original_outcome, op.deadline_elapsed = op.state, True
            transition(op, State.TIMED_OUT)
            op.reason = Reason.APPLY_TIMEOUT
            self._save(op, {"observation": self.backend.facts})
            self.fallback_after = None  # once per failed move: not again on this start
            log.warning("undoing the failed move on %s not confirmed within the deadline", undone)
        elif self.engine._expired(op):
            op.original_outcome, op.deadline_elapsed = op.state, True
            transition(op, State.TIMED_OUT)
            op.reason = Reason.APPLY_TIMEOUT
            self._save(op, {"observation": self.backend.facts})
            if self.move is not None and self.move.get("target") == (self.bootstrap or {}).get(
                "bssid"
            ):
                self.move["result"] = "revert not confirmed within the deadline"
            self.mode = MULTI_AP
            self.bssid, self.station = self.configured, self.configured_station
            self.moves += 1
            self._keep_target()
            if asked:
                log.warning("backhaul revert not confirmed within the deadline; given up")
            else:
                self.fallback_after = None  # once per failed switch: not again on this start
                log.warning("undoing the failed switch not confirmed within the deadline; given up")

    def _settle(self, op, snap):
        """Confirm or time out the latest operation; hold on failure."""
        if op.intent.get("mode") == BOOTSTRAP:
            return self._settle_revert(op, snap)
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
            # another station's configuration is not this switch's to judge: cm on another
            # station is followed (_follow_cm), not held
            mine = self.backend.active == op.intent.get("station")
            if same_start and mine and not self.engine._matches(snap.config, target):
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
                        "mode": self.mode,
                        "moves": self.moves,
                    }
                )
            )

    def _record_bootstrap(self):
        """The BSS the station is on before any EMOSA move is its bootstrap upstream (spec 8.6):
        cm's own 3-address association to the onboarding/GTP BSS, over which the GRE runs. Recorded
        once, when first observed on a working uplink with no move made, and persisted, so a later
        steer back to it is recognised as a revert across restarts. If EMOSA first binds while the
        pod is already on a parent (no clean bootstrap), none is recorded, and a revert then falls
        back to the pod's onboarding SSID seen in a scan (spec 8.6)."""
        if self.bootstrap is not None or self.moves or self.move is not None:
            return
        facts = self.backend.facts or {}
        parent = facts.get("parent")
        if not facts.get("kind") or not parent:
            return
        self.bootstrap = {"bssid": parent, "ssid": facts.get("ssid")}
        self.bootstrap_path.write_text(json.dumps(self.bootstrap) + "\n")
        log.info("bootstrap upstream recorded: %s (%s)", parent, facts.get("ssid"))

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
        self.mode = move.get("previous_mode", MULTI_AP)
        self.moves += 1
        self._keep_target()
        other = op.intent.get("mode") == GRE and op.intent.get("station") != self.station
        if self.bssid is None or other:
            # a GRE child's move from its bootstrap: no upstream to go back to; or a GRE move onto
            # another station than the one the uplink goes back to, which the switch back leaves
            # enabled to join its target late and take cm (V54 bench, 17 October; a Multi-AP
            # switch back disables it): the agent undoes its own write (_undo) while the station
            # still carries it on this start
            self.fallback_after = {"instance": (op.plan or {}).get("instance"), "intent": op.intent}
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
        self.mode = MULTI_AP  # the configured upstream's kind (a GRE child's configured is None)
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
        # Steer only from a working EasyMesh backhaul EMOSA manages: a Multi-AP pod on its
        # Multi-AP backhaul, or a GRE child on its GRE path (the GTP, or a parent). A Multi-AP
        # pod still on its bootstrap GRE (the GTP) is NOT steered: it reaches its own backhaul
        # first (spec 8.3). A GRE child has no Multi-AP configured upstream.
        kind = (self.backend.facts or {}).get("kind")
        on_backhaul = kind == MULTI_AP or (
            kind == GRE and (self.configured is None or self.mode == GRE)
        )
        if not on_backhaul:
            return "not_on_easymesh_backhaul"
        op = self.latest()
        if op is not None and op.state in ACTIVE:
            return "switch_in_progress"
        if (
            self.bootstrap
            and bssid == self.bootstrap["bssid"]
            and self.bssid != self.bootstrap["bssid"]
        ):
            # a steer back to the pod's own bootstrap BSS is a revert, not a pin (spec 8.6), but
            # only when EMOSA has moved the station away from it: a steer to the bootstrap while the
            # station is still on it is an ordinary confirm-in-place and falls through to the switch
            # path below (spec 8.3, the re-switch-to-the-BSS-in-use case). EMOSA undoes its move
            # (restores the station's vconf snapshot, deletes its credential) and the pod's parent
            # pin lands the station back on the bootstrap. (With no record the revert would be
            # recognised by the onboarding SSID in a scan; not built yet.)
            previous, previous_station, previous_mode = self.bssid, self.station, self.mode
            self.move = {
                "target": bssid,
                "previous": previous,
                "station": self.station,
                "previous_station": previous_station,
                "mode": BOOTSTRAP,
                "previous_mode": previous_mode,
                "result": None,
            }
            self.mode, self.bssid = BOOTSTRAP, self.configured
            self.moves += 1
            self._keep_target()
            log.info("backhaul revert requested: %s -> bootstrap %s", previous, bssid)
            return None
        if bssid in {(self.backend.facts or {}).get("mac")}:
            return "own_station"
        station = self.station if band is None else self.backend.station_for(band)
        if station is None:
            return "no_station_on_band"
        fixed = self.backend.fixed_channel(station)
        if fixed is not None and channel is not None and channel != fixed:
            return "channel_not_operable"
        # A target in the fleet's GRE parent set is a 3-address GRE join (spec 8.6); any
        # other target is a Multi-AP (4-address) join. The GRE path is only ever taken for a
        # known fleet parent: a child must never try GRE onto a foreign AP (7f, 11 Oct).
        mode = GRE if bssid in self.parents() else MULTI_AP
        previous, previous_station, previous_mode = self.bssid, self.station, self.mode
        self.move = {
            "target": bssid,
            "previous": previous,
            "station": station,
            "previous_station": previous_station,
            "mode": mode,
            "previous_mode": previous_mode,
            "result": None,
        }
        if (bssid, station, mode) != (previous, previous_station, previous_mode):
            self.bssid, self.station, self.mode = bssid, station, mode
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
            and facts.get("kind") == move["mode"]
            and facts.get("station") == move["station"]
            and facts.get("parent") == move["target"]
        ):
            move["result"] = True
            return True
        return None

    def _undo(self, snap):
        """A GRE child's failed switch (spec 8.3 "Moved", 8.6): with no configured upstream to
        switch to, the agent undoes its own write as the revert to the bootstrap does, while the
        station still carries the failed switch's credential on that start. None when there is
        nothing left to undo (a new start, or the credential gone) or no snapshot to restore."""
        failed = self.fallback_after
        if failed is None:
            return None
        intent = UplinkIntent(**failed["intent"])
        config = self.backend.configs.get(intent.station, snap.config)
        if failed["instance"] != self.backend.instance or not self.engine._matches(
            config, intent.target(self.engine.vault)
        ):
            self.fallback_after = None
            return None
        restore = self._move_snapshot(intent.station)
        if restore is None or self.bootstrap is None:
            return None
        if not failed.get("undo"):
            failed["undo"] = True
            log.info("the failed switch's credential is still on %s: undoing it", intent.station)
        return UplinkIntent(
            self.pod_id,
            intent.station,
            "",
            "",
            mode=BOOTSTRAP,
            bssid=self.bootstrap["bssid"],
            restore=restore,
        )

    def _wanted(self, op, snap):
        """The intent to request now, or None (``self.waiting`` says why)."""
        if self.held() and not self._hold_over():
            self.waiting = "held on option 2"
            return None
        if not snap.ready:
            self.waiting = "pod not bound"
            return None
        if self.mode == BOOTSTRAP:
            # a revert in flight: write back the move's snapshot and drop EMOSA's credential, until
            # the station is back on the bootstrap (then _settle resets the mode and this stops);
            # the station the uplink is on stays enabled, whatever its snapshot says
            restore = self._move_snapshot(self.station)
            if restore is None or self.bootstrap is None:
                self.waiting = "revert without a snapshot"
                return None
            intent = UplinkIntent(
                self.pod_id,
                self.station,
                "",
                "",
                mode=BOOTSTRAP,
                bssid=self.bootstrap["bssid"],
                restore={**restore, "enabled": True} if "enabled" in restore else restore,
            )
        else:
            credentials = self.credentials()
            if credentials is None:
                self.waiting = "no EasyMesh backhaul credentials"
                return None
            failed = (self.fallback_after or {}).get("intent") or {}
            undo = None
            if self.bssid is None or failed.get("station") not in (None, self.station):
                # a failed switch's credential off its station first (_undo): a GRE child with
                # no configured upstream then writes nothing until the controller steers it onto
                # a parent, its bootstrap uplink (the GTP) standing; after a failed GRE move onto
                # another station, the uplink's own station is pinned again after the undo
                undo = self._undo(snap)
            if undo is not None:
                intent = undo
            elif self.bssid is None:
                self.waiting = "no upstream (awaiting a Backhaul Steering Request)"
                return None
            else:
                intent = UplinkIntent(
                    self.pod_id, self.station, *credentials, mode=self.mode, bssid=self.bssid
                )
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
        if self.fallback_after is not None and intent.mode != BOOTSTRAP:
            # A switch written while cm still acts on the failed one is reverted with it
            # (rdk-1004, 5 Oct 2026): first the station off the failed credential, or a new
            # start. Bounded: OpenSync restarts a pod left without a working uplink.
            failed = self.fallback_after
            config = self.backend.configs.get(failed["intent"].get("station"), snap.config)
            if failed["instance"] == self.backend.instance and self.engine._matches(
                config, UplinkIntent(**failed["intent"]).target(self.engine.vault)
            ):
                self.waiting = "the pod returning to its bootstrap uplink"
                return None
            self.fallback_after = None
        if not self.settled():
            self.waiting = "fronthaul not settled"
            return None
        self.waiting = None
        return intent

    def _new_start(self):
        """A new start of the pod's OpenSync: a finished move's record is the last start's (only
        the kept target carries over), so the status no longer shows it."""
        instance = self.backend.instance
        if instance == self._instance:
            return
        if self._instance is not None and self.move is not None and self.move["result"] is not None:
            self.move = None
        self._instance = instance

    async def _make_before_break(self, op):
        """Spec 8.3, 8.6: a GRE move's station on its target with its gretap up takes the station
        cm uses and any other EMOSA wrote off (``take_off``), once per switch. cm keeps a working
        uplink in use and re-selects among equal ones only at its periodic pass (2 minutes on the
        Pis, 17 October): only with the old station gone does it take the new one at once."""
        # the switch this process wrote (_written), under way or applied already (cm may have
        # taken the new gretap by itself): not an older one, after the agent started again
        active = (State.CONFIG_COMMITTED, State.INDETERMINATE, State.OBSERVED_APPLIED)
        if op is None or op.state not in active or op.operation_id != self._written:
            return
        if op.intent.get("mode") != GRE or op.operation_id in self._taken_off:
            return
        if (op.plan or {}).get("instance") != self.backend.instance:
            return
        station, target = op.intent.get("station"), op.intent.get("bssid")
        states = self.backend.stations_state
        own = states.get(station) or {}
        gretap = own.get("gretap") or {}
        if own.get("parent") != target or not (gretap.get("has_L2") and gretap.get("has_L3")):
            return
        self._taken_off.add(op.operation_id)
        # the station cm uses (the uplink's until now) and any EMOSA wrote: not the pod's own other
        # stations (a fallback station is the pod's, spec 8.6)
        active = self.backend.active
        others = sorted(
            n
            for n, s in states.items()
            if n != station and s.get("enabled") and (n == active or s.get("emosa"))
        )
        if not others:
            return
        restores = {n: self._move_snapshot(n) for n in others}
        try:
            off = await self.backend.take_off(others, restores)
        except (EmosaError, ConnectionError, TimeoutError) as exc:
            self._taken_off.discard(op.operation_id)  # again on the next tick, until the deadline
            log.warning("make-before-break: taking %s off failed: %s", ", ".join(others), exc)
            return
        if off:
            log.info(
                "%s on %s with its gretap up: %s taken off (make-before-break)",
                station,
                target,
                ", ".join(off),
            )

    async def _undo_without_snapshot(self):
        """A failed GRE move onto another station with no snapshot of it to put back (or no
        bootstrap recorded): that station taken off instead (``take_off``), once."""
        failed = self.fallback_after
        if failed is None or failed["instance"] != self.backend.instance:
            return
        station = failed["intent"].get("station")
        if station == self.station or (
            self._move_snapshot(station) is not None and self.bootstrap is not None
        ):
            return
        self.fallback_after = None
        try:
            off = await self.backend.take_off([station], {})
        except (EmosaError, ConnectionError, TimeoutError) as exc:
            log.warning("the failed move's station %s not taken off: %s", station, exc)
            return
        if off:
            log.info("the failed move's station %s taken off (no snapshot to restore)", station)

    def _follow_cm(self, op):
        """Spec 8.3: a GRE child's uplink record follows the station cm uses. cm on another bound
        station's own gretap (the pod's own fallback, or cm's periodic pass) puts the uplink on
        that station: on EMOSA's pin there when it carries one, else, on the same start, on its
        bootstrap (no EMOSA upstream; on a new start the kept target is switched to instead).
        Nothing is written for it; the reported backhaul already names that station, so the
        controller learns it from the pod's next topology."""
        if op is not None and op.state in ACTIVE:
            return
        if self.mode == BOOTSTRAP or self.fallback_after is not None:
            return
        if self.move is not None and self.move["result"] is None:
            return  # the controller's move under way: the record is its target until it settles
        if self.configured is not None and self.mode != GRE:
            return
        active, facts = self.backend.active, self.backend.facts or {}
        if active == self.station or facts.get("in_use") != f"g-{active}":
            return
        config = self.backend.configs.get(active) or {}
        if config.get("uplink") == GRE and config.get("bssid"):
            bssid, mode = config["bssid"], GRE
        elif op is not None and (op.plan or {}).get("instance") == self.backend.instance:
            bssid, mode = self.configured, MULTI_AP
        else:
            return
        log.warning(
            "cm uses %s (parent %s), not %s: the uplink record follows it",
            active,
            facts.get("parent"),
            self.station,
        )
        self.station, self.bssid, self.mode = active, bssid, mode
        self.moves += 1
        self._keep_target()

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
        self._new_start()
        self._record_bootstrap()
        await self._make_before_break(self.latest())
        await self._undo_without_snapshot()
        self._follow_cm(self.latest())
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
        self._written = op.operation_id
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
            "bootstrap": self.bootstrap,
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


def m2_backhaul(*stores):
    """The backhaul BSS (SSID, secret reference) of the pod's applied M2 sets: the fronthaul
    scope's first, then each further radio's (spec 9: a pod whose fronthaul radio maps no backhaul
    BSS, the Pis' adapter B, has it only in its 5 GHz radio's set). In each scope the latest
    applied set decides."""
    for store in stores:
        for op in reversed(store.operations()):
            if op.state == State.OBSERVED_APPLIED:
                found = next(
                    (
                        (bss["ssid"], bss["secret_ref"])
                        for bss in op.intent.get("additional") or ()
                        if bss.get("role") == "backhaul"
                    ),
                    None,
                )
                if found:
                    return found
                break
    return None
