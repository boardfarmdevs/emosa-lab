#!/bin/bash
# Watch pod-6's uplink switch: once applied, restart OpenSync on the pod and
# watch the agent apply it again on the new start. Prints each change.
V=emosa-osl-0923
status() {
    lxc exec $V -- /snap/bin/lxc exec emosa -- python3 -c "
import json; s=json.load(open('/var/lib/emosa/MVXPOD023674CB574D/status.json')); u=s['uplink']; o=u.get('operation') or {}
print(o.get('instance'), o.get('state'), u.get('uplink'), 'held' if u.get('held') else '-', (s['session'] or {}).get('state'))" 2>/dev/null
}
last= first= restarted=
for _ in $(seq 400); do
    r=$(status)
    [ "$r" != "$last" ] && echo "$(date +%T) $r"
    last=$r
    case "$r" in *held*) exit 1 ;; esac
    set -- $r
    if [ "$2" = OBSERVED_APPLIED ] && [ "$3" = multi-ap ]; then
        if [ -z "$first" ]; then
            first=$1
            sleep 20
            echo "$(date +%T) restarting OpenSync on pod-6"
            lxc exec $V -- /snap/bin/lxc exec pod-6 -- sh -c 'nohup /usr/opensync/bin/restart.sh >/tmp/restart.log 2>&1 &'
        elif [ "$1" != "$first" ]; then
            echo "$(date +%T) applied again on a new start"
            exit 0
        fi
    fi
    sleep 2
done
exit 2
