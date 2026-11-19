"""Stop the containers of the deployed candil-brain app, so the next request starts on the new version.

Only the deployed app's own containers: a library download or index build runs as a separate (ephemeral) app and
must never be stopped by a deploy. If the app list cannot be read, nothing is stopped."""
import json, subprocess

apps = json.loads(subprocess.run(['modal', 'app', 'list', '--json'], capture_output=True, text=True).stdout or '[]')
key = lambda d, *names: next((d[n] for n in names if n in d), None)
ids = {key(a, 'app_id', 'App ID') for a in apps
       if key(a, 'description', 'Description') == 'candil-brain' and str(key(a, 'state', 'State')).lower() == 'deployed'}
ids.discard(None)
print('deployed app:', ids or 'not found, so nothing is stopped')
conts = json.loads(subprocess.run(['modal', 'container', 'list', '--json'], capture_output=True, text=True).stdout or '[]')
for c in conts:
    if ids and key(c, 'app_id', 'App ID') in ids:
        cid = key(c, 'container_id', 'Container ID')
        print('stopping', cid)
        subprocess.run(['modal', 'container', 'stop', '--yes', cid])
    else:
        print('leaving', key(c, 'container_id', 'Container ID'), key(c, 'app_id', 'App ID'))
