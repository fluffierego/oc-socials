# OC Socials

This version is built for a Discord RP server. Members sign in with Discord, then create/use their own OCs across three fake social apps:

- Instagram
- Twitter/X
- iMessage group chats

## What members can do

- Sign in with their Discord account.
- Create multiple OCs.
- Customize each OC's name, @username, avatar, banner and bio.
- Post on Instagram or Twitter/X as any of their OCs.
- Reply to posts as any of their OCs.
- Create iMessage group chats.
- Put multiple OCs from the same Discord account into a group chat.
- Send each message as whichever of their OCs they choose.
- Keep other members' OCs separate by Discord account.

The optional `/tupperbox/resolve` endpoint is ready for a separate Discord/Tupperbox bridge later. The web app itself does not impersonate Discord users or expose Discord tokens.

## Discord OAuth setup

In Discord Developer Portal for your application:

1. Open **OAuth2**.
2. Under **Redirects**, add exactly:
   `http://127.0.0.1:5050/oauth/callback`
3. Use the Client ID shown on the OAuth2 page.
4. Use the Client Secret from the same page.
5. Put those values into the environment before starting Flask.

For local testing, from Terminal inside this folder:

```bash
source .venv/bin/activate
export FLASK_SECRET="make-this-a-long-random-value"
export DISCORD_CLIENT_ID="YOUR_CLIENT_ID"
export DISCORD_CLIENT_SECRET="YOUR_CLIENT_SECRET"
export DISCORD_REDIRECT_URI="http://127.0.0.1:5050/oauth/callback"
python app.py
```

Then open:

`http://127.0.0.1:5050`

For other people in your Discord server to access it from outside your Mac, the Flask server must be hosted on a reachable HTTPS URL (and the Discord redirect URI must be changed to that URL). `127.0.0.1` is only your Mac.

## Important about Tupperbox

Tupperbox itself remains the Discord proxy. This site gives each Discord member their own OC identities inside the fake social apps. The optional Tupperbox bridge endpoint can be connected to a Discord bot later if you want Discord/Tupperbox activity to be mirrored into the website automatically.

Do not put your Discord Client Secret or bot token into HTML, JavaScript, screenshots, or anything you share publicly.
