const SUPABASE_URL = "https://hbaadljcliqzwjtpobex.supabase.co";
const SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhiYWFkbGpjbGlxendqdHBvYmV4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3NTgxMDY5NywiZXhwIjoyMDkxMzg2Njk3fQ.eLXIQHJd1QEUH2WmGWlB4doMO7osLkO7ljIGTs_IRj0";
const HIDDEN_SHEET_NAME = "SupabaseItems";
const DATA_START_ROW = 5;
const COL_UNIT   = 2;
const COL_COLD   = 4;
const COL_COLE   = 5;
const COL_RATE   = 6;
const COL_AMOUNT = 7;
const HEADER_ROW = 4;

// ─── MAIN FUNCTION (manual run) ───────────────────────────────────────────────
function fetchAndSetup() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getActiveSheet();
  if (!sheet || sheet.isSheetHidden() || sheet.getName() === HIDDEN_SHEET_NAME) {
    sheet = ss.getSheets().find(s => !s.isSheetHidden() && s.getName() !== HIDDEN_SHEET_NAME);
  }
  if (!sheet) { Browser.msgBox("No visible sheet found."); return; }

  const partyName = sheet.getRange("A1").getValue().toString().trim();
  if (!partyName) { Browser.msgBox("Cell A1 is empty."); return; }

  ss.toast("Fetching rates for: " + partyName, "Working...", 30);

  const itemRateMap = fetchLatestRates(partyName);
  const itemCount = Object.keys(itemRateMap).length;
  if (itemCount === 0) { Browser.msgBox("No items found for: " + partyName); return; }

  ss.toast("Found " + itemCount + " items. Setting up...", "Working...", 30);

  storeItemsInHiddenSheet(ss, itemRateMap);
  fillRatesFromColD(sheet, itemRateMap);
  setDropdownOnColE(ss, sheet);
  installOnEditTrigger();

  ss.toast("Done! " + itemCount + " items loaded.", "✅ Success", 5);
}

// ─── FETCH LATEST RATES VIA RPC ───────────────────────────────────────────────
function fetchLatestRates(partyName) {
  const url = `${SUPABASE_URL}/rest/v1/rpc/get_latest_rates_for_party`;
  const response = UrlFetchApp.fetch(url, {
    method: "POST",
    headers: {
      "apikey": SUPABASE_KEY,
      "Authorization": "Bearer " + SUPABASE_KEY,
      "Content-Type": "application/json"
    },
    payload: JSON.stringify({ p_party_name: partyName }),
    muteHttpExceptions: true
  });

  const rows = JSON.parse(response.getContentText());
  const itemRateMap = {};
  if (!Array.isArray(rows)) return itemRateMap;
  rows.forEach(row => {
    const name = row.stock_item_name ? row.stock_item_name.trim() : null;
    const rate = parseFloat(row.rate) || 0;
    if (name) itemRateMap[name] = rate;
  });
  return itemRateMap;
}

// ─── STORE ITEMS IN HIDDEN SHEET ─────────────────────────────────────────────
function storeItemsInHiddenSheet(ss, itemRateMap) {
  const existing = ss.getSheetByName(HIDDEN_SHEET_NAME);
  if (existing) ss.deleteSheet(existing);
  const hiddenSheet = ss.insertSheet(HIDDEN_SHEET_NAME);
  hiddenSheet.hideSheet();
  hiddenSheet.getRange("A1").setValue("Item Name");
  hiddenSheet.getRange("B1").setValue("Rate");
  const entries = Object.entries(itemRateMap).sort((a, b) => a[0].localeCompare(b[0]));
  if (entries.length > 0) hiddenSheet.getRange(2, 1, entries.length, 2).setValues(entries);
}

// ─── AUTO-FILL RATE FROM COL D ───────────────────────────────────────────────
function fillRatesFromColD(sheet, itemRateMap) {
  const lastRow = sheet.getLastRow();
  const colLetter = n => String.fromCharCode(64 + n);

  for (let r = DATA_START_ROW; r <= lastRow; r++) {
    const itemName = sheet.getRange(r, COL_COLD).getValue().toString().trim();
    if (!itemName || itemName === "NO MATCH") continue;
    if (itemRateMap.hasOwnProperty(itemName)) {
      const rate = Math.round(itemRateMap[itemName]);
      sheet.getRange(r, COL_RATE).setValue(rate);
      sheet.getRange(r, COL_AMOUNT).setFormula(
      `=IFERROR(VALUE(REGEXEXTRACT(${colLetter(COL_UNIT)}${r},"^[0-9]+")),1)*${colLetter(COL_RATE)}${r}`      );
    }
  }
}

// ─── SET DROPDOWN ON COL E ────────────────────────────────────────────────────
function setDropdownOnColE(ss, sheet) {
  const lastRow = sheet.getLastRow();
  sheet.getRange(DATA_START_ROW, COL_COLE, lastRow - DATA_START_ROW + 1, 1).clearDataValidations();
  SpreadsheetApp.flush();

  const hiddenSheet = ss.getSheetByName(HIDDEN_SHEET_NAME);
  const itemCount = hiddenSheet.getLastRow() - 1;
  if (itemCount <= 0) return;

  const rule = SpreadsheetApp.newDataValidation()
    .requireValueInRange(hiddenSheet.getRange(2, 1, itemCount, 1), true)
    .setAllowInvalid(true)
    .build();

  for (let r = DATA_START_ROW; r <= lastRow; r++) {
    sheet.getRange(r, COL_COLE).setDataValidation(rule);
  }
}

// ─── onEdit TRIGGER ───────────────────────────────────────────────────────────
function onEdit(e) {
  const sheet = e.range.getSheet();
  if (sheet.isSheetHidden() || sheet.getName() === HIDDEN_SHEET_NAME) return;

  const row = e.range.getRow();
  const col = e.range.getColumn();
  if (col !== COL_COLE || row < DATA_START_ROW) return;

  const selectedItem = e.range.getValue().toString().trim();
  if (!selectedItem) return;

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const hiddenSheet = ss.getSheetByName(HIDDEN_SHEET_NAME);
  if (!hiddenSheet) return;

  const colLetter = n => String.fromCharCode(64 + n);
  const data = hiddenSheet.getDataRange().getValues();

  for (let i = 1; i < data.length; i++) {
    if (data[i][0].toString().trim() === selectedItem) {
      const rate = Math.round(data[i][1]);
      sheet.getRange(row, COL_RATE).setValue(rate);
      sheet.getRange(row, COL_AMOUNT).setFormula(
        `=IFERROR(VALUE(REGEXEXTRACT(${colLetter(COL_UNIT)}${row},"^[0-9]+")),1)*${colLetter(COL_RATE)}${row}`
      );
      return;
    }
  }
}

// ─── INSTALL onEdit TRIGGER ───────────────────────────────────────────────────
function installOnEditTrigger() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === "onEdit") ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger("onEdit").forSpreadsheet(ss).onEdit().create();
}

// ─── SUPABASE FETCH HELPER ────────────────────────────────────────────────────
function supabaseFetch(url) {
  return UrlFetchApp.fetch(url, {
    method: "GET",
    headers: {
      "apikey": SUPABASE_KEY,
      "Authorization": "Bearer " + SUPABASE_KEY,
      "Content-Type": "application/json"
    },
    muteHttpExceptions: true
  }).getContentText();
}

// ─── doPost: called by n8n ────────────────────────────────────────────────────
function doPost(e) {
  try {
    const payload = JSON.parse(e.postData.contents);
    const partyName = payload.party_name;
    const rows = payload.rows;

    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = ss.getSheetByName("Challan");

    sheet.getRange("A1").setValue(partyName);
    sheet.getRange("A5:G500").clearContent();
    sheet.getRange(5, COL_UNIT, rows.length, 1).setNumberFormat('@STRING@');

    // Copy format from template
    sheet.getRange(5, 1, 19, 7).copyTo(
      sheet.getRange(5, 1, rows.length, 7),
      SpreadsheetApp.CopyPasteType.PASTE_FORMAT,
      false
    );

    // Write A-D
    const values = rows.map(r => [r.line_no, r.qty_text, r.minicpm_read, r.stock_matched]);
    sheet.getRange(5, 1, values.length, 4).setValues(values);

    // Fetch rates
    const itemRateMap = fetchLatestRates(partyName);
    if (Object.keys(itemRateMap).length > 0) {
      storeItemsInHiddenSheet(ss, itemRateMap);
      fillRatesFromColD(sheet, itemRateMap);
      installOnEditTrigger();
    }

    // Clear Col E validation completely before writing values
    sheet.getRange(5, COL_COLE, rows.length, 1).clearDataValidations();
    SpreadsheetApp.flush();

    // Write Col E values freely
    const colEValues = rows.map(r => [r.stock_matched]);
    sheet.getRange(5, COL_COLE, rows.length, 1).setValues(colEValues);

    // Apply dropdown AFTER values are written
    if (Object.keys(itemRateMap).length > 0) {
      setDropdownOnColE(ss, sheet);
    }

    return ContentService.createTextOutput(JSON.stringify({ status: "success" }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch(err) {
    return ContentService.createTextOutput(JSON.stringify({ status: "error", message: err.message }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}



const WEBHOOK_URL = "https://profligately-frugal-laila.ngrok-free.dev/webhook-test/b5cc150a-1a7c-475d-8ee5-1120c5c04a5a";
// ☝️ Use production URL (without -test) when workflow is active

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("Challan")
    .addItem("✅ Done - Send to Invoice", "submitDone")
    .addToUi();
}

function submitDone() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName("Challan");

  const partyName = sheet.getRange("A1").getValue().toString().trim();
  if (!partyName) { Browser.msgBox("Party name missing in A1"); return; }

  const lastRow = sheet.getLastRow();
  const items = [];

  for (let r = DATA_START_ROW; r <= lastRow; r++) {
    const stockName = sheet.getRange(r, COL_COLE).getValue().toString().trim(); // Col E
    if (!stockName || stockName === "NO MATCH") continue;

    const qtyText  = sheet.getRange(r, COL_UNIT).getValue().toString().trim();  // Col B
    const rate     = parseFloat(sheet.getRange(r, COL_RATE).getValue()) || 0;   // Col F
    const amount   = parseFloat(sheet.getRange(r, COL_AMOUNT).getValue()) || 0; // Col G

    // Extract leading number from qty_text e.g. "10 NOS" → 10
    const qtyMatch = qtyText.match(/^(\d+(\.\d+)?)/);
    const quantity = qtyMatch ? parseFloat(qtyMatch[1]) : 1;

    items.push({ stock_item_name: stockName, quantity, rate, amount });
  }

  if (items.length === 0) { Browser.msgBox("No items found to send."); return; }

  const payload = JSON.stringify({ party_name: partyName, items });

  const options = {
    method: "POST",
    contentType: "application/json",
    payload: payload,
    muteHttpExceptions: true
  };

  ss.toast("Sending to n8n...", "Working", 10);
  const response = UrlFetchApp.fetch(WEBHOOK_URL, options);
  ss.toast("Sent! Response: " + response.getResponseCode(), "✅ Done", 5);
}