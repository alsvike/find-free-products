# Publish Gratisjagten for free

The website is hosted by Netlify, while GitHub Actions runs the Python crawler.
Netlify itself cannot run an overnight process: scheduled functions are limited
to 30 seconds and background functions to 15 minutes.

This setup stays at **0 kr.** when:

- the GitHub repository is public, which makes standard GitHub-hosted Actions
  runners free;
- the site remains within Netlify's 300 monthly Free-plan credits; and
- you do not enable a paid Netlify plan or paid GitHub runner.

The tradeoff is that the crawler source, input domains, and result CSV files are
public on GitHub. Never commit credentials.

## 1. Put this folder on GitHub

Create a **public** GitHub repository named `find-free-products` and push the
complete project. Keep GitHub Actions enabled. The included workflow runs every
night at 22:00 Europe/Copenhagen and can also be started from the website.

Before pushing, edit `site/config.js` and replace `YOUR_GITHUB_USERNAME` with
your GitHub username. Change the repository name there too if you use a
different name.

The workflow runs for up to five hours, saves progress continuously, and uses
`--resume` the following night. GitHub-hosted jobs have a six-hour ceiling, so
the shorter crawler limit leaves time to finish active shops and commit data.

In the repository settings, open **Actions → General → Workflow permissions**
and select **Read and write permissions** if the workflow cannot push updated
CSV files.

## 2. Create the dashboard's GitHub token

Create a fine-grained GitHub personal access token restricted to this one
repository. Give it **Actions: Read and write** permission. It only dispatches
the workflow and reads its status; the token must never be placed in
`site/config.js` or committed to Git.

Keep the token ready for the Netlify environment variables below.

## 3. Connect the repository to Netlify

1. In Netlify, choose **Add new project → Import an existing project**.
2. Select the GitHub repository.
3. Netlify reads `netlify.toml`; no build command is needed.
4. Publish the site on the **Free** plan.

Netlify deploys the `site` folder. Nightly data commits are configured to skip
new production deploys; the dashboard reads current CSV files directly from
the public GitHub repository. This avoids spending 15 Netlify credits every
night.

## 4. Add private environment variables in Netlify

In **Project configuration → Environment variables**, add:

- `GITHUB_OWNER`: your GitHub username
- `GITHUB_REPO`: `find-free-products` or your chosen repository name
- `GITHUB_BRANCH`: `main`
- `GITHUB_DISPATCH_TOKEN`: the fine-grained token from step 2
- `DASHBOARD_ADMIN_KEY`: a long private password used only by the Start button

Redeploy once after adding these variables so the two small control functions
receive them. Do not expose these values in `site/config.js`.

## 5. Start the first overnight run

Open the Netlify dashboard, enter your private dashboard key, choose
**Overnight · 5 hours**, and press **Start crawler**. The latest GitHub run and
its log link appear underneath. The initial data already contains the cleaned
200-shop test snapshot, so the first run resumes instead of starting from zero.

You can still start it from **GitHub → Actions → Nightly free-product crawl →
Run workflow** if the dashboard controls have not been configured yet.

## Operational notes

- Retryable statuses such as `rate_limited`, `blocked`, and `unreachable` are
  attempted again on the next run.
- Completed shops are skipped.
- Findings are deduplicated across repeated runs.
- Result and status CSV files can be downloaded from the dashboard.
- Netlify's Free plan has a hard monthly credit limit, so it cannot create an
  unexpected bill; the site pauses if the allowance is exhausted.
- Never add private credentials or tokens to the CSV files or website folder.
