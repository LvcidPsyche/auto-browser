# Login once and save an auth profile

Use this when you want a reusable logged-in session.

## Flow

1. Create a session.
2. Navigate to the login page.
3. Take over manually if MFA or CAPTCHA appears.
4. Save the auth profile.
5. Start future sessions from that profile.

## Example

```bash
curl -s http://127.0.0.1:8000/sessions \
  -X POST \
  -H 'content-type: application/json' \
  -d '{"name":"outlook-login","start_url":"https://outlook.live.com/mail/0/"}' | jq
```

After manual login:

```bash
curl -s http://127.0.0.1:8000/sessions/<session-id>/auth-profiles \
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
