"""Credentials are environment/local settings, never source constants or API readbacks."""
import json
import os
import secrets
from pathlib import Path

SECRET_KEYS = {'password','email_password','wechat_token','token','api_key','access_key_secret'}

def credentials():
    username, password = os.environ.get('BIDMONITOR_USERNAME'), os.environ.get('BIDMONITOR_PASSWORD')
    if username or password:
        if not username or not password:
            raise RuntimeError('请同时配置 BIDMONITOR_USERNAME 和 BIDMONITOR_PASSWORD')
        return username, password
    file = Path(__file__).with_name('auth.local.json')
    if not file.exists():
        try:
            fd = os.open(str(file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump({'username':'admin','password':secrets.token_urlsafe(24)}, out)
    data = json.loads(file.read_text(encoding='utf-8'))
    if not data.get('username') or not data.get('password'):
        raise RuntimeError('本机认证配置无效')
    return data['username'], data['password']

def redacted(value):
    if isinstance(value, dict):
        return {k: ('***' if v else '') if k in SECRET_KEYS else redacted(v) for k,v in value.items()}
    if isinstance(value, list):
        return [redacted(x) for x in value]
    return value

def preserve_secrets(incoming, previous):
    if isinstance(incoming, dict):
        previous = previous if isinstance(previous, dict) else {}
        return {k: (previous.get(k, '') if v in ('',None,'***') else v) if k in SECRET_KEYS
                else preserve_secrets(v,previous.get(k)) for k,v in incoming.items()}
    if isinstance(incoming, list):
        previous = previous if isinstance(previous, list) else []
        result=[]
        for i, value in enumerate(incoming):
            old = previous[i] if i < len(previous) else None
            # Contacts use a persistent ID so renaming/reordering cannot mix credentials.
            if isinstance(value, dict) and value.get('id'):
                old = next((p for p in previous if isinstance(p,dict) and p.get('id')==value['id']), None)
            result.append(preserve_secrets(value,old))
        return result
    return incoming
