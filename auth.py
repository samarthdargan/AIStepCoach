from google_auth_oauthlib.flow import InstalledAppFlow
import pickle

SCOPES = ['https://www.googleapis.com/auth/fitness.activity.read']

flow = InstalledAppFlow.from_client_secrets_file(
    'credentials.json', SCOPES)

creds = flow.run_local_server(port=0)

with open('token.pkl', 'wb') as f:
    pickle.dump(creds, f)

print("✅ Auth done! token.pkl saved.")
