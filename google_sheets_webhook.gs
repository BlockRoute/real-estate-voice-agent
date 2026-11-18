// Appends each call from the voice agent to the "Voice Agent Call Log" sheet.
// Paste into the sheet's Extensions > Apps Script, deploy as a Web app, then set in .env:
//   LEAD_WEBHOOK_URL=<web app URL>?token=<TOKEN below>
// functions.py posts the log row plus the transcript to that URL after every call.

const TOKEN = "SET_ME"; // stops strangers who find the URL from writing rows. Real value lives only in Apps Script and .env

function doPost(e) {
  if (e.parameter.token !== TOKEN) {
    return ContentService.createTextOutput("forbidden");
  }
  const data = JSON.parse(e.postData.contents);
  const transcript = (data.transcript || [])
    .map(t => (t.role === "assistant" ? "Agent: " : "Caller: ") + t.content)
    .join("\n")
    .slice(0, 45000); // a cell holds at most 50,000 characters
  data.transcript = transcript;

  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  if (data.lead_phone) data.lead_phone = "'" + data.lead_phone; // keep "+1..." as text, not a number
  sheet.appendRow(headers.map(h => (data[h] === undefined || data[h] === null ? "" : String(data[h]))));
  return ContentService.createTextOutput("ok");
}
