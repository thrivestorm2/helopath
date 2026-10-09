# Putting the tracker online (DigitalOcean App Platform)

The setup is the same as mediccred. DigitalOcean watches the `main` branch and redeploys on every merge. Pull
requests are where changes are tested: GitHub runs the full test suite on each one and shows a ✓ or ✗ before you
merge. GitHub needs no DigitalOcean token or secrets.

**Cost:** about **$25/month**. That's the website ($5), the background worker ($5) and the database ($15).

---

## One-time setup

### 1. Merge the pull request

Open the pull request, wait for the **Tests** check to show a green ✓, then click **Merge pull request**.

### 2. Create the database

In DigitalOcean, go to **Databases → Create Database Cluster** and choose:

| Setting | Choose |
|---|---|
| Engine | **PostgreSQL 16** |
| Datacenter | **New York** (any NYC) |
| Plan | **Basic**, 1 GB RAM / 1 vCPU / 10 GB disk (about $15/month) |
| Name | **`helopath-db`** (exactly; the app looks for this name) |

Click **Create** and wait until it says **Online** (about 5 minutes).

### 3. Create the app from the spec

Do this the same way you did for mediccred: **Apps → Create App**, then import the spec file **`.do/app.yaml`**
from the `helopath` repository (branch `main`).

If the wizard only offers to detect components, let it, and finish creating the app. Then go to the app's
**Settings → App Spec → Edit**, replace everything with the contents of `.do/app.yaml`, and save. Either way you
should end up with three things in the app:
- **web**, the website;
- **worker**, which does the background jobs;
- **db**, attached to `helopath-db`.

### 4. Set your admin password

In the app, go to **Settings → App-Level Environment Variables → Edit** and add:

| Key | Value | Encrypt |
|---|---|---|
| `HELOPATH_ADMIN_PASSWORD` | a password you choose (12+ characters) | ✓ |

Save. The app redeploys by itself. When it's done, the address at the top of the app page
(`https://helopath-xxxxx.ondigitalocean.app`) shows the login page.

You don't need anything else. Unlike mediccred's `AUTH_SECRET`, the app's internal secret key is derived
automatically. The database connection and its certificate are wired up by the spec.

### 5. First login and loading the data

1. Log in as **`admin`** with that password.
2. Open **Operations** (top menu). "Background worker" should say **running**.
3. Press, in order:
   1. **Load FAA registry** (a few minutes).
   2. **Seed FAA sites** (a minute or two). If it fails, upload the FAA NASR `APT_BASE.csv` with the file chooser
      next to the button.
   3. Review the hospital helipads (the link is on the page). You can carry on while the next step runs.
   4. **Start 90-day backfill** (about 8–9 hours). You can close the browser.

After that, new days are added every evening automatically.

---

## Everyday use

- **Making changes:** open a pull request, check that **Tests** shows ✓, then merge. DigitalOcean deploys `main`
  within a few minutes.
- **Optional safety net:** in GitHub go to **Settings → Branches → Add rule** for `main`, then tick "Require
  status checks to pass" and select **test**. GitHub then won't let a red pull request merge.
- **Change your password:** go to **Admin** (top menu), then **Change password**.
- **Add a person who can only look:** go to **Admin → Users → Add user**, then add the group **Viewer**.
- **Add another admin:** do the same, but also tick **Staff status** and add the group **Admin**.
- **Apply site edits:** go to **Operations → Rebuild**.

## If something goes wrong

- **A deploy failed:** in DigitalOcean go to **Apps → helopath → Activity**, click the failed deployment and
  read the build or deploy log. The live site keeps running the previous version.
- **"Background worker: not responding":** go to **Apps → helopath → Runtime Logs** and choose **worker**.
- **The login page never appears after the first deploy:** check that the database is named exactly
  `helopath-db` and is **Online**.

## Stopping

To stop paying, delete **Apps → helopath** *and* **Databases → helopath-db**. Deleting the database erases all
data permanently.
