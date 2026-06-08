# Setup: connecting ytmetrics to your YouTube channel

This is the full, click-by-click walkthrough for the one-time Google setup. It takes about
5 minutes and is all done in a browser. When you're done you'll have a
`secrets/client_secret.json` file and an authorized token, and `ytmetrics` will be able to
pull your channel's analytics.

The tool can't do these steps for you — they involve your Google account and consent.

## Before you start: which Google account?

Two different accounts can be involved; don't mix them up.

- **The account that owns the channel** is what matters for *data access*. Use it when you
  authorize (the `list-channels` step at the end). It must be the channel **Owner** —
  required if you want **revenue** data. A Manager role usually works for analytics but is
  unreliable for revenue; Editor/Viewer roles can't see revenue at all.
- **The Google Cloud project** (steps 1–4) can be created under *any* account — it just
  issues the credential file. It has no bearing on which channel's data you can read.

Simplest path: do everything signed in as the channel's **owner** account.

> If your channel is a **Brand Account** managed by several people, sign in with an account
> that has the **Owner** role, and when the browser consent screen appears, pick the brand
> channel (not your personal one).

---

## 1. Create a Google Cloud project (no billing needed)

- Go to **https://console.cloud.google.com/projectcreate**
- **Project name:** `ytmetrics` (anything) → **Create**
- Wait for the "Creating project" notification, then make sure the new project is selected
  in the top-bar project picker.

> You will **not** be asked for billing — the YouTube APIs are free and quota-limited, so
> leaving billing off makes the cost ceiling provably $0.

## 2. Enable the two APIs

Open each link (with your project selected) and click the blue **Enable** button:

- **YouTube Analytics API** → https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com
- **YouTube Data API v3** → https://console.cloud.google.com/apis/library/youtube.googleapis.com

## 3. Configure the consent screen ("Google Auth Platform")

- Go to **https://console.cloud.google.com/auth/overview** → **Get started**
- **App Information:** App name `ytmetrics`, User support email = your email → **Next**
- **Audience:** choose **External** → **Next**
- **Contact Information:** your email → **Next**
- Check the agree box → **Create**

Then add your channel-owner account as a test user:

- Go to **https://console.cloud.google.com/auth/audience**
- Under **Test users** → **+ Add users** → enter **the email of the account that owns the
  channel** (the one you'll authorize with) → **Save**

> Leave the Publishing status as **Testing**. Test users bypass Google's verification even
> for the analytics/revenue scopes — you do not need to "publish" or get verified.

## 4. Create the Desktop OAuth client

- Go to **https://console.cloud.google.com/auth/clients** → **+ Create client**
  *(older UI: APIs & Services → Credentials → + Create credentials → OAuth client ID)*
- **Application type:** **Desktop app**
- **Name:** `ytmetrics-desktop` → **Create**
- In the confirmation dialog, click **Download JSON** (or the ⬇ icon next to the client
  in the list afterward)

## 5. Put the JSON where the tool expects it

It downloads as something like `client_secret_xxxx.apps.googleusercontent.com.json`. Move
and rename it into the project's `secrets/` folder:

```bash
mkdir -p secrets
mv ~/Downloads/client_secret_*.apps.googleusercontent.com.json secrets/client_secret.json
```

(`secrets/` is gitignored — it will never be committed.)

## 6. Authorize and verify

From the project directory:

```bash
uv run ytmetrics list-channels    # opens a browser to approve read-only access
```

- Sign in as the **channel owner** account (pick the brand channel if prompted).
- You'll see **"Google hasn't verified this app."** That's expected for a Testing app —
  click **Advanced → Go to ytmetrics (unsafe) → Continue**.
- Approve the read-only access (and revenue, if `include_revenue = true`).

`list-channels` prints your channel id(s) and titles. **Confirm the id/title is the right
channel.** If it shows the wrong (e.g. personal) channel, you authorized the wrong
identity — fix it with:

```bash
rm secrets/token_main.json && uv run ytmetrics list-channels
```

Optionally paste the `UC…` id into `config.toml` (replacing `channel_id = "mine"`).

Now confirm the whole chain and do a first pull:

```bash
uv run ytmetrics doctor --live    # checks auth, scopes, API reachability, db
uv run ytmetrics pull --days 7
uv run ytmetrics info
```

## Troubleshooting

| symptom | fix |
|---|---|
| **"Access blocked: app is in testing"** during consent | The account you're signing in with isn't on the test-user list (step 3). Add it. |
| `list-channels` shows the **wrong channel** | You authorized the wrong identity. `rm secrets/token_main.json` and re-run, choosing the right account/brand. |
| **`revenue` empty / degraded** every pull | The channel isn't monetized (yet), or you authorized as a non-Owner. Use the Owner account; revenue only exists once you're in the YouTube Partner Program. |
| **`client secret not found`** | The JSON isn't at the path in `config.toml` (`secrets/client_secret.json` by default). |
| **`doctor --live` fails to resolve the channel** | Re-check that both APIs are enabled (step 2) and that the token was minted for an account with access to the channel. |
