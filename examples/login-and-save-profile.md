# Login once and save an auth profile

Use this when you want a reusable logged-in session.

## Flow

1. Create a session.
2. Navigate to the login page.
3. Take over manually if MFA or CAPTCHA appears.
4. Save the auth profile.
5. Start future sessions from that profile.

## Example

Create a session and capture the session id:

```bash
SESSION_ID=$(
  curl -s http://127.0.0.1:8000/sessions \
    -X POST \
    -H 'content-type: application/json' \
    -d '{"name":"outlook-login","start_url":"https://outlook.live.com/mail/0/"}' \
  | jq -r '.id'
)

echo "$SESSION_ID"
```

If the login flow needs MFA, CAPTCHA, or manual recovery, request takeover:

```bash
curl -s "http://127.0.0.1:8000/sessions/$SESSION_ID/takeover" \
  -X POST \
  -H 'content-type: application/json' \
  -d '{"reason":"Complete manual login"}' | jq
```

You can also open the local takeover UI directly:

```text
http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale
```

If you just want the raw create-session request:

```bash
curl -s http://127.0.0.1:8000/sessions \
  -X POST \
  -H 'content-type: application/json' \
  -d '{"name":"outlook-login","start_url":"https://outlook.live.com/mail/0/"}' | jq
```

After manual login:

```bash
curl -s "http://127.0.0.1:8000/sessions/$SESSION_ID/auth-profiles" \
  -X POST \
  -H 'content-type: application/json' \
  -d '{"profile_name":"outlook-default"}' | jq
```

Resume later:

```bash
curl -s http://127.0.0.1:8000/sessions \
  -X POST \
  -H 'content-type: application/json' \
  -d '{"name":"outlook-reuse","start_url":"https://outlook.live.com/mail/0/","auth_profile":"outlook-default"}' | jq
```

Optional sanity check:

```bash
curl -s http://127.0.0.1:8000/auth-profiles | jq
```
