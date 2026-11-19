"""Stop the containers of the deployed candil-brain app, so the next request starts on the new version.

Only the deployed app's own containers: a library download or index build runs as a separate (ephemeral) app and
must never be stopped by a deploy. If the app list cannot be read, nothing is stopped."""
import json, subprocess

apps = json.loads(subprocess.run(['modal', 'app', 'list', '--json'], capture_output=True, text=True).stdout or '[]')
for a in apps:
    if 'candil' in json.dumps(a):
        print('app', json.dumps(a))
ids = {a.get('App ID') for a in apps
       if 'candil-brain' in str(a.get('Description')) and 'deployed' in str(a.get('State')).lower()}
ids.discard(None)
print('deployed app:', ids or 'not found, so nothing is stopped')
conts = json.loads(subprocess.run(['modal', 'container', 'list', '--json'], capture_output=True, text=True).stdout or '[]')
for c in conts:
    if ids and (c.get('App ID') or c.get('app_id')) in ids:
        cid = c.get('Container ID') or c.get('container_id')
        print('stopping', cid)
        subprocess.run(['modal', 'container', 'stop', '--yes', cid])
    else:
        print('leaving', c.get('Container ID') or c.get('container_id'), c.get('App ID') or c.get('app_id'))
