/**
 * Adds "CRM Sync > 🔄 Refresh Data Now" to the live spreadsheet. Clicking it starts
 * the GitHub Actions workflow that rebuilds the sheet from Pipedrive (~2-3 minutes).
 * The same workflow runs automatically on the 1st of each month.
 *
 * SETUP (once, by the sheet owner)
 *  1. Create a fine-grained GitHub token (github.com > Settings > Developer settings >
 *     Fine-grained tokens), ideally on a shared/bot account rather than a personal one:
 *       - Repository access: Only select repositories > crm-sheets-etl-pipeline
 *       - Permissions: Actions = Read and write (nothing else)
 *       - Expiration: set one, and note the date for renewal.
 *  2. In the live sheet: Extensions > Apps Script. Paste this file, save.
 *  3. Project Settings (gear icon) > Script Properties > Add properties:
 *       GITHUB_TOKEN = <the token>
 *       LIVE_SPREADSHEET_ID = <the live sheet's ID, from its URL: /spreadsheets/d/<ID>/edit>
 *  4. Reload the sheet. The first click asks each person to authorize the script
 *     (it needs permission to "connect to an external service": GitHub).
 *
 * SECURITY: anyone with EDIT access to this spreadsheet can open the script and read
 * the token. That is why the token can only start this one repository's workflows.
 * Viewers (no edit access) cannot see it.
 */

const REPO = 'your-username/crm-sheets-etl-pipeline';
const WORKFLOW_FILE = 'pipedrive-upload.yml';
const BRANCH = 'main';

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('CRM Sync')
    .addItem('🔄 Refresh Data Now', 'refreshDataNow')
    .addToUi();
}

function refreshDataNow() {
  const ui = SpreadsheetApp.getUi();
  const props = PropertiesService.getScriptProperties();

  // Only the live sheet may trigger a refresh. Archive copies carry this script (and its
  // properties) too; a click there must not rewrite the live sheet unexpectedly.
  const liveId = props.getProperty('LIVE_SPREADSHEET_ID');
  if (!liveId) {
    ui.alert('Refresh is not set up yet: LIVE_SPREADSHEET_ID is missing from the script properties.');
    return;
  }
  if (SpreadsheetApp.getActiveSpreadsheet().getId() !== liveId) {
    ui.alert('This is not the live report (probably an archive copy). Open the live ' +
             '"Portfolio & Pipeline" sheet to refresh.');
    return;
  }

  const token = props.getProperty('GITHUB_TOKEN');
  if (!token) {
    ui.alert('Refresh is not set up yet: GITHUB_TOKEN is missing from the script properties.');
    return;
  }

  const choice = ui.alert(
    'Refresh data from Pipedrive?',
    'The whole sheet will be rebuilt from Pipedrive. It takes about 2-3 minutes; ' +
    'reload the page afterwards to see the new data.',
    ui.ButtonSet.OK_CANCEL);
  if (choice !== ui.Button.OK) {
    return;
  }

  const url = 'https://api.github.com/repos/' + REPO + '/actions/workflows/' + WORKFLOW_FILE + '/dispatches';
  const response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + token,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    // Same as a normal run: no forced write, no extra archive copy.
    payload: JSON.stringify({ ref: BRANCH, inputs: { force: 'false', archive: 'false', dry_run: 'false' } }),
    muteHttpExceptions: true,
  });

  const runs = 'https://github.com/' + REPO + '/actions/workflows/' + WORKFLOW_FILE;
  const code = response.getResponseCode();
  if (code === 204) {
    ui.alert('Refresh started. Reload this page in about 3 minutes.\n\nProgress: ' + runs);
  } else if (code === 401 || code === 403) {
    ui.alert('GitHub rejected the token (HTTP ' + code + '). It may have expired or lack ' +
             'the "Actions: Read and write" permission. Ask the sheet owner to renew it.');
  } else {
    ui.alert('Refresh could not start (HTTP ' + code + '):\n' + response.getContentText().slice(0, 300));
  }
}
