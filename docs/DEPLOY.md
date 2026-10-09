# Putting the tracker online (DigitalOcean App Platform)

You do this once. It takes about 15 minutes of clicking, then 20–30 minutes of waiting. After that, every change
merged into `main` goes live by itself, but only if all the automated tests pass first.

**Cost:** about **$25/month**. That covers the website ($5), the background worker ($5) and the database ($15).
How to stop paying is at the bottom of this page.

---

## Step 1 — DigitalOcean account

1. Sign up or log in at <https://cloud.digitalocean.com> and add a payment method.

## Step 2 — Let DigitalOcean read the code on GitHub

1. In DigitalOcean, click the green **Create** button (top right), then **App Platform**.
2. Under "Create from source code", choose **GitHub**, then click **Install & Authorize** (or **Edit your GitHub
   permissions**).
3. GitHub opens. Choose **Only select repositories**, pick **helopath**, and click **Install & Authorize**.
4. Back in DigitalOcean, **close the page**. Don't finish creating an app here, because the automatic deploy
   creates it for you.

## Step 3 — Create a DigitalOcean API token

1. In DigitalOcean's left sidebar, click **API** (near the bottom).
2. Click **Generate New Token**.
   - **Name:** `helopath-github`
   - **Expiration:** **No expiry**. If you choose a date, deploys stop working when it passes.
   - **Scopes:** **Full Access**
3. Click **Generate Token** and **copy it now**. DigitalOcean shows it only once.

## Step 4 — Give GitHub the two secrets

1. Open the repository on GitHub: <https://github.com/thrivestorm2/helopath>
2. Click **Settings** (top tab), then in the left sidebar **Secrets and variables → Actions**.
3. Click **New repository secret** and add:

   | Name (exactly) | Secret |
   |---|---|
   | `DIGITALOCEAN_ACCESS_TOKEN` | the token from Step 3 |
   | `HELOPATH_ADMIN_PASSWORD` | a password you make up for your admin login (12+ characters) |

## Step 5 — Turn it on

1. Open the pull request named **"Deploy to DigitalOcean App Platform"** and click **Merge pull request**, then
   **Confirm merge**.
2. Click the **Actions** tab. A run called **Deploy** is in progress (a yellow dot).
   - The first run takes **20–30 minutes**, because it creates the database. Later runs take about 5–10 minutes.
   - A green check means it's live. Click the run, and the summary at the bottom shows your address, like
     `https://helopath-xxxxx.ondigitalocean.app`. Bookmark it.

## Step 6 — First login and loading the data

1. Open your address and log in as **`admin`** with the password from Step 4.
2. Click **Operations** in the top menu. The "Background worker" box should say **running**.
3. Press the buttons in order. Each one shows its progress in "Recent tasks"; refresh the page to update it.
   1. **Load FAA registry** (a few minutes).
   2. **Seed FAA sites** (a minute or two). If it fails, download the FAA "NASR subscription" CSV file and pick it
      with the file chooser next to the button, then press it again.
   3. Review the hospital helipads (the link is on the page). You can carry on while the next step runs.
   4. **Start 90-day backfill** (about 8–9 hours). You can close the browser; it keeps running.
4. After that, everything is automatic. New days are added every evening.

## Everyday tasks

- **Change your password:** click **Admin** (top menu), then **Change password** (top right).
- **Add a person who can only look:** go to **Admin → Users → Add user**. Set a username and password, then on
  the next page add the group **Viewer** and save.
- **Add another admin:** do the same as above, but also tick **Staff status** and add the group **Admin**.
- **Apply site edits:** go to **Operations** and press **Rebuild**.

## If something goes wrong

- **Red ✗ on a Deploy run:** click it, then click the step with the red ✗. The first error lines usually say
  what's wrong.
  - *"Missing repository secret(s)"*: redo Step 4 and check the names are spelled exactly.
  - *GitHub / repository access errors*: redo Step 2.
  - *"Unable to authenticate" / 401*: the token expired or was mistyped. Make a new one (Step 3) and replace the
    secret.
  - **A test failed:** nothing was deployed and the live site is unchanged. That's the safety net working. The
    change needs fixing first.
- **"Background worker: not responding"** for more than 15 minutes: in DigitalOcean, open **Apps → helopath →
  Runtime Logs** and choose **worker** to see what it says.
- **Run a deploy by hand:** go to **Actions → Deploy → Run workflow**.

## Stopping and starting

- **To stop paying:** in DigitalOcean, delete **Apps → helopath** *and* **Databases → helopath-db**. Deleting the
  database erases all data permanently.
- **To start again later:** go to **Actions → Deploy → Run workflow**. This creates everything fresh.
