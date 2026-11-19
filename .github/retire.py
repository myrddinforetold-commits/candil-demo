"""Stop the containers of the deployed candil-brain app, so the next request starts on the new version.

Only the deployed app's own containers: a library download or index build runs as a separate (ephemeral) app and
must never be stopped by a deploy. If the app list cannot be read, nothing is stopped."""
import json, subprocess

apps = json.loads(subprocess.run(['modal', 'app', 'list', '--json'], capture_output=True, text=True).stdout or '[]')
ids = {a.get('App ID') or a.get('app_id') for a in apps
       if (a.get('Description') or a.get('name')) == 'candil-brain' and str(a.get('State') or a.get('state')).lower() == 'deployed'}
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
