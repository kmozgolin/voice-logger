import json
from pathlib import Path

cfg = json.loads(Path('config.json').read_text())
print('gdrive_folder_id:', repr(cfg.get('gdrive_folder_id')))
print('credentials.json exists:', Path('credentials.json').exists())
print('token.json exists:', Path('token.json').exists())

folder_id = cfg.get('gdrive_folder_id', '')
if not folder_id:
    print()
    print('PROBLEM: gdrive_folder_id is empty in config.json')
    print('Fix: open dashboard -> Settings -> Google Drive ID -> paste folder ID -> Save')
elif not Path('credentials.json').exists():
    print()
    print('PROBLEM: credentials.json not found')
    print('Fix: download OAuth JSON from Google Cloud Console,')
    print('     rename to credentials.json, put in voice-logger folder')
elif not Path('token.json').exists():
    print()
    print('INFO: token.json not found - authorization needed')
    print('Running authorization flow...')
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        SCOPES = ['https://www.googleapis.com/auth/drive.file']
        flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
        creds = flow.run_local_server(port=0)
        Path('token.json').write_text(creds.to_json())
        print('Authorization successful! token.json saved.')
    except Exception as e:
        print(f'Authorization failed: {e}')
else:
    print()
    print('All files present. Testing upload...')
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        import tempfile, os

        creds = Credentials.from_authorized_user_file('token.json')
        svc = build('drive', 'v3', credentials=creds)

        # Create test file
        tmp = Path(tempfile.mktemp(suffix='.txt'))
        tmp.write_text('voice-logger test upload')

        meta = {'name': 'voice-logger-test.txt', 'parents': [folder_id]}
        media = MediaFileUpload(str(tmp), mimetype='text/plain')
        result = svc.files().create(body=meta, media_body=media, fields='id').execute()
        tmp.unlink()

        print(f'Upload OK! File ID: {result.get("id")}')
        print('Check your Google Drive VoiceLogs folder.')
    except Exception as e:
        print(f'Upload failed: {e}')

input('\nPress Enter to close...')
