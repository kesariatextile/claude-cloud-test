/**
 * Kesaria Textile - Marketing Reporting & Dashboard
 *
 * Bind this script to a blank Google Sheet (the master "Marketing Dashboard"),
 * reload the sheet and use the "Reporting" menu -> "1. Setup everything".
 *
 * What it builds:
 *  - One Google Sheet per person per team (Daily Log + Summary [+ Performance Log for YouTube SEO])
 *  - Master dashboard: Config, All_Data, YT_Performance, Dashboard, YT Dashboard
 *  - Hourly auto-sync trigger that pulls every person's data into the master sheet
 */

// ---------------------------------------------------------------- CONFIG
const FOLDER_NAME = 'Marketing Reporting - Kesaria Textile';

// Set `email` to share a person's sheet with them as editor (optional).
const TEAMS = [
  {
    label: 'YouTube SEO',
    hasChannel: true,
    catLabel: 'Content Type',
    categories: ['Long Video', 'Short Video', 'Community Post'],
    people: [
      { name: 'Himanshu', channels: 5, email: '' },
      { name: 'Rohit', channels: 9, email: '' },
      { name: 'Vijay', channels: 9, email: '' },
      { name: 'Shivam', channels: 9, email: '' },
      { name: 'Arun', channels: 1, email: '' },
      { name: 'Yagna', channels: 1, email: '' },
    ],
  },
  {
    label: 'Graphic Designer',
    catLabel: 'Graphic Category',
    categories: [
      'Website Banner', 'Blog Cover - Franchise', 'Blog Cover - Wholesale', 'GMB Update',
      'Social Reel Cover', 'Social Post - Single', 'Social Post - Carousel', 'Standee',
      'Wall Banner', 'Visiting Card', 'Invitation Pamphlet', 'Offline Printable - Billbook',
      'Offline Printable - Golfcart', 'Offline Printable - LED Banner', 'Offline Printable - ID Card',
      'Offline Printable - Other', 'Company Info Post', 'Other',
    ],
    people: [
      { name: 'Kapil', email: '' }, { name: 'Tanushree', email: '' },
      { name: 'Kalpana', email: '' }, { name: 'Urvashi', email: '' },
    ],
  },
  {
    label: 'Video Editor',
    catLabel: 'Video Category',
    subLabel: 'Video Type',
    categories: ['Wholesale', 'Franchise', 'Founder Branding', 'Offline Branding'],
    subTypes: ['Long Video', 'Short Video'],
    people: [
      { name: 'Rahul', email: '' }, { name: 'Lucky', email: '' }, { name: 'Rajesh', email: '' },
      { name: 'Pushpa', email: '' }, { name: 'Dhiraj', email: '' }, { name: 'Ansh', email: '' },
    ],
  },
  {
    label: 'Website SEO',
    catLabel: 'Task Category',
    categories: ['Blog Posted', 'Backlink Created', 'GMB Update', 'GMB Service', 'Optimized Listing'],
    people: [
      { name: 'Arun', email: '' }, { name: 'Yagna', email: '' }, { name: 'Ramkesh', email: '' },
    ],
  },
  {
    label: 'Social Media Executive',
    catLabel: 'Platform',
    categories: [
      'Threads', 'YT Community', 'WhatsApp Community', 'WhatsApp Status', 'Facebook Channel',
      'Instagram Broadcast', 'Twitter - Company', 'Twitter - Founder',
      'LinkedIn - Company', 'LinkedIn - Founder',
    ],
    people: [{ name: 'Khush', email: '' }],
  },
];

const YT_TEAM = 'YouTube SEO';
const PERIODS = ['Daily', 'Weekly', 'Monthly', 'Last Month', 'Last 6 Months', 'Yearly'];
const PERIOD_START = [
  '=TODAY()',
  '=TODAY()-WEEKDAY(TODAY(),2)+1',
  '=EOMONTH(TODAY(),-1)+1',
  '=EOMONTH(TODAY(),-2)+1',
  '=EDATE(TODAY(),-6)+1',
  '=DATE(YEAR(TODAY()),1,1)',
];
const PERIOD_END = ['=TODAY()', '=TODAY()', '=TODAY()', '=EOMONTH(TODAY(),-1)', '=TODAY()', '=TODAY()'];
const MAX_ROWS = 5000;
const BLUE = '#1a73e8';

// ---------------------------------------------------------------- MENU
function onOpen() {
  SpreadsheetApp.getUi().createMenu('Reporting')
    .addItem('1. Setup everything', 'setupAll')
    .addSeparator()
    .addItem('Create missing person sheets', 'createPersonSheets')
    .addItem('Sync data now', 'syncData')
    .addItem('Rebuild dashboards', 'buildDashboards')
    .addItem('Enable hourly auto-sync', 'installTrigger')
    .addToUi();
}

function setupAll() {
  createPersonSheets();
  syncData();
  buildDashboards();
  installTrigger();
  SpreadsheetApp.getActive().toast('Setup complete. Person sheet links are in the Config tab.', 'Reporting', 10);
}

// ---------------------------------------------------------------- HELPERS
function colLetter_(n) { return String.fromCharCode(64 + n); }
function personKey_(team, person) { return team.label + '|' + person.name; }

function getConfigSheet_() {
  const ss = SpreadsheetApp.getActive();
  let sh = ss.getSheetByName('Config');
  if (!sh) {
    sh = ss.insertSheet('Config');
    sh.getRange(1, 1, 1, 6).setValues([['Key', 'Team', 'Person', 'Spreadsheet ID', 'Link', 'Channels']])
      .setBackground(BLUE).setFontColor('#fff').setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  return sh;
}

function getFolder_() {
  const it = DriveApp.getFoldersByName(FOLDER_NAME);
  return it.hasNext() ? it.next() : DriveApp.createFolder(FOLDER_NAME);
}

function channelList_(person) {
  const out = [];
  for (let i = 1; i <= person.channels; i++) out.push('CH' + i);
  return out;
}

function writePeriodHeader_(sh, row) {
  sh.getRange(row, 1, 3, 1).setValues([['Period'], ['From'], ['To']]).setFontWeight('bold');
  sh.getRange(row, 2, 1, 6).setValues([PERIODS]).setFontWeight('bold').setHorizontalAlignment('center');
  sh.getRange(row + 1, 2, 1, 6).setFormulas([PERIOD_START]);
  sh.getRange(row + 2, 2, 1, 6).setFormulas([PERIOD_END]);
  sh.getRange(row + 1, 2, 2, 6).setNumberFormat('dd-mmm-yy').setHorizontalAlignment('center');
  sh.getRange(row, 1, 3, 7).setBackground('#e8f0fe');
}

/** Writes a titled table (label + 6 period columns). Returns the next free row. */
function writeTable_(sh, row, title, firstHeader, labels, fn, withTotal) {
  sh.getRange(row, 1).setValue(title).setFontWeight('bold').setFontSize(12);
  const h = row + 1;
  sh.getRange(h, 1, 1, 7).setValues([[firstHeader].concat(PERIODS)])
    .setBackground(BLUE).setFontColor('#ffffff').setFontWeight('bold');
  const data = labels.map(function (lb, i) {
    const r = h + 1 + i;
    const arr = [lb];
    for (let c = 2; c <= 7; c++) arr.push(fn(r, lb, colLetter_(c)));
    return arr;
  });
  if (data.length) sh.getRange(h + 1, 1, data.length, 7).setValues(data);
  let end = h + data.length;
  if (withTotal && data.length) {
    const tot = ['Total'];
    for (let c = 2; c <= 7; c++) tot.push('=SUM(' + colLetter_(c) + (h + 1) + ':' + colLetter_(c) + end + ')');
    sh.getRange(end + 1, 1, 1, 7).setValues([tot]).setFontWeight('bold').setBackground('#f1f3f4');
    end++;
  }
  sh.getRange(h + 1, 2, Math.max(data.length, 1) + (withTotal ? 1 : 0), 6).setNumberFormat('#,##0').setHorizontalAlignment('center');
  return end + 2;
}

// ---------------------------------------------------------------- PERSON SHEETS
function createPersonSheets() {
  const cfg = getConfigSheet_();
  const existing = {};
  cfg.getDataRange().getValues().slice(1).forEach(function (r) { if (r[3]) existing[r[0]] = true; });
  const folder = getFolder_();
  const started = Date.now();
  let created = 0;

  TEAMS.forEach(function (team) {
    team.people.forEach(function (person) {
      const key = personKey_(team, person);
      if (existing[key]) return;
      if (Date.now() - started > 4.5 * 60 * 1000) return; // stay inside the 6 min limit; re-run to continue
      const ss = buildPersonSheet_(folder, team, person);
      cfg.appendRow([key, team.label, person.name, ss.getId(), ss.getUrl(), person.channels || '']);
      created++;
    });
  });
  SpreadsheetApp.getActive().toast(created + ' person sheet(s) created. If some are missing, run this again.', 'Reporting', 8);
}

function buildPersonSheet_(folder, team, person) {
  const ss = SpreadsheetApp.create(person.name + ' - ' + team.label + ' Reporting');
  DriveApp.getFileById(ss.getId()).moveTo(folder);

  // ---- Daily Log
  const log = ss.getSheets()[0].setName('Daily Log');
  const headers = ['Date', team.catLabel, team.subLabel || 'Type', 'Channel', 'Qty (blank = 1)', 'Title / Description', 'Link'];
  log.getRange(1, 1, 1, 7).setValues([headers]).setBackground(BLUE).setFontColor('#fff').setFontWeight('bold').setWrap(true);
  log.setFrozenRows(1);
  const rule = function () { return SpreadsheetApp.newDataValidation(); };
  log.getRange(2, 1, MAX_ROWS, 1).setDataValidation(rule().requireDate().setAllowInvalid(false)
    .setHelpText('Enter a valid date').build()).setNumberFormat('dd-mmm-yyyy');
  log.getRange(2, 2, MAX_ROWS, 1).setDataValidation(rule().requireValueInList(team.categories, true).setAllowInvalid(false).build());
  if (team.subTypes) {
    log.getRange(2, 3, MAX_ROWS, 1).setDataValidation(rule().requireValueInList(team.subTypes, true).setAllowInvalid(false).build());
  } else {
    log.hideColumns(3);
  }
  if (team.hasChannel) {
    log.getRange(2, 4, MAX_ROWS, 1).setDataValidation(rule().requireValueInList(channelList_(person), true).setAllowInvalid(false).build());
  } else {
    log.hideColumns(4);
  }
  log.getRange(2, 5, MAX_ROWS, 1).setDataValidation(rule().requireNumberGreaterThanOrEqualTo(0).setAllowInvalid(false).build());
  log.setColumnWidths(1, 1, 100);
  log.setColumnWidths(2, 3, 150);
  log.setColumnWidth(5, 90);
  log.setColumnWidth(6, 280);
  log.setColumnWidth(7, 320);

  // ---- Performance Log (YouTube SEO only)
  if (team.hasChannel) {
    const perf = ss.insertSheet('Performance Log');
    perf.getRange(1, 1, 1, 7).setValues([['Date Recorded', 'Channel', 'Video Link', 'New / Old Video', 'Views', 'Comments', 'Subscribers']])
      .setBackground(BLUE).setFontColor('#fff').setFontWeight('bold');
    perf.setFrozenRows(1);
    perf.getRange(2, 1, MAX_ROWS, 1).setDataValidation(rule().requireDate().setAllowInvalid(false).build()).setNumberFormat('dd-mmm-yyyy');
    perf.getRange(2, 2, MAX_ROWS, 1).setDataValidation(rule().requireValueInList(channelList_(person), true).setAllowInvalid(false).build());
    perf.getRange(2, 4, MAX_ROWS, 1).setDataValidation(rule().requireValueInList(['New Video', 'Old Video'], true).setAllowInvalid(false).build());
    perf.getRange(2, 5, MAX_ROWS, 3).setDataValidation(rule().requireNumberGreaterThanOrEqualTo(0).setAllowInvalid(false).build());
    perf.setColumnWidths(1, 2, 110);
    perf.setColumnWidth(3, 320);
    perf.setColumnWidths(4, 4, 120);
  }

  buildSummary_(ss, team, person);

  if (person.email) ss.addEditor(person.email);
  return ss;
}

function buildSummary_(ss, team, person) {
  const sh = ss.insertSheet('Summary', 0);
  sh.getRange('A1').setValue(person.name + ' - ' + team.label + ' Summary').setFontSize(14).setFontWeight('bold');
  writePeriodHeader_(sh, 3);
  const L = "'Daily Log'!";
  const crit = function (cl) {
    return L + '$A:$A,">="&' + cl + '$4,' + L + '$A:$A,"<="&' + cl + '$5';
  };

  let row = writeTable_(sh, 7, 'Count by ' + team.catLabel, team.catLabel, team.categories, function (r, lb, cl) {
    return '=SUMIFS(' + L + '$E:$E,' + L + '$B:$B,$A' + r + ',' + crit(cl) + ')' +
      '+COUNTIFS(' + L + '$B:$B,$A' + r + ',' + L + '$E:$E,"",' + crit(cl) + ')';
  }, true);

  if (team.hasChannel) {
    row = writeTable_(sh, row, 'Count by Channel (all content types)', 'Channel', channelList_(person), function (r, lb, cl) {
      return '=SUMIFS(' + L + '$E:$E,' + L + '$D:$D,$A' + r + ',' + crit(cl) + ')' +
        '+COUNTIFS(' + L + '$D:$D,$A' + r + ',' + L + '$B:$B,"<>",' + L + '$E:$E,"",' + crit(cl) + ')';
    }, true);

    const P = "'Performance Log'!";
    [['Views', 'E'], ['Comments', 'F'], ['Subscribers', 'G']].forEach(function (m) {
      row = writeTable_(sh, row, m[0] + ' - New vs Old Videos', 'Video Age', ['New Video', 'Old Video'], function (r, lb, cl) {
        return '=SUMIFS(' + P + '$' + m[1] + ':$' + m[1] + ',' + P + '$D:$D,$A' + r + ',' +
          P + '$A:$A,">="&' + cl + '$4,' + P + '$A:$A,"<="&' + cl + '$5)';
      }, true);
    });
  }
  sh.setColumnWidth(1, 220);
  sh.setColumnWidths(2, 6, 105);
  sh.setFrozenRows(5);
}

// ---------------------------------------------------------------- SYNC
function syncData() {
  const dash = SpreadsheetApp.getActive();
  const cfg = getConfigSheet_().getDataRange().getValues().slice(1);
  const rows = [];
  const perf = [];
  const errors = [];

  cfg.forEach(function (r) {
    const team = r[1], person = r[2], id = r[3];
    if (!id) return;
    try {
      const ss = SpreadsheetApp.openById(id);
      const log = ss.getSheetByName('Daily Log');
      if (log && log.getLastRow() > 1) {
        log.getRange(2, 1, log.getLastRow() - 1, 7).getValues().forEach(function (v) {
          if (v[0] instanceof Date && v[1]) {
            rows.push([v[0], team, person, v[1], v[2], v[3], v[4] === '' ? 1 : Number(v[4]), v[5], v[6]]);
          }
        });
      }
      const p = ss.getSheetByName('Performance Log');
      if (p && p.getLastRow() > 1) {
        p.getRange(2, 1, p.getLastRow() - 1, 7).getValues().forEach(function (v) {
          if (v[0] instanceof Date && v[1]) perf.push([v[0], person, v[1], v[2], v[3], Number(v[4]) || 0, Number(v[5]) || 0, Number(v[6]) || 0]);
        });
      }
    } catch (e) {
      errors.push(team + ' / ' + person + ': ' + e.message);
    }
  });

  rows.sort(function (a, b) { return a[0] - b[0]; });
  perf.sort(function (a, b) { return a[0] - b[0]; });
  writeData_(dash, 'All_Data', ['Date', 'Team', 'Person', 'Category', 'Type', 'Channel', 'Qty', 'Title / Description', 'Link'], rows);
  writeData_(dash, 'YT_Performance', ['Date', 'Person', 'Channel', 'Video Link', 'New / Old Video', 'Views', 'Comments', 'Subscribers'], perf);

  const cfgSheet = getConfigSheet_();
  cfgSheet.getRange('H1').setValue('Last sync');
  cfgSheet.getRange('H2').setValue(new Date()).setNumberFormat('dd-mmm-yyyy hh:mm');
  cfgSheet.getRange('H3').setValue(errors.length ? 'Errors: ' + errors.join(' | ') : 'No errors');
}

function writeData_(ss, name, headers, rows) {
  let sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  sh.clear();
  sh.getRange(1, 1, 1, headers.length).setValues([headers]).setBackground(BLUE).setFontColor('#fff').setFontWeight('bold');
  sh.setFrozenRows(1);
  if (rows.length) sh.getRange(2, 1, rows.length, headers.length).setValues(rows);
  sh.getRange(2, 1, Math.max(rows.length, 1), 1).setNumberFormat('dd-mmm-yyyy');
  if (sh.getFilter()) sh.getFilter().remove();
  sh.getRange(1, 1, Math.max(rows.length + 1, 2), headers.length).createFilter();
}

function installTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'syncData') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('syncData').timeBased().everyHours(1).create();
}

// ---------------------------------------------------------------- DASHBOARDS
function buildDashboards() {
  buildMainDashboard_();
  buildYtDashboard_();
}

function resetSheet_(name, index) {
  const ss = SpreadsheetApp.getActive();
  let sh = ss.getSheetByName(name);
  if (sh) { sh.getCharts().forEach(function (c) { sh.removeChart(c); }); sh.clear(); }
  else sh = ss.insertSheet(name, index);
  return sh;
}

function buildMainDashboard_() {
  const sh = resetSheet_('Dashboard', 0);
  sh.getRange('A1').setValue('Kesaria Textile - Marketing Team Dashboard').setFontSize(18).setFontWeight('bold');
  sh.getRange('A2').setFormula('="Data last synced: "&TEXT(Config!H2,"dd-mmm-yyyy hh:mm")&"   |   Yearly = calendar year (Jan-Dec)"').setFontColor('#5f6368');
  writePeriodHeader_(sh, 3);

  const A = 'All_Data!';
  const base = function (cl) {
    return A + '$A:$A,">="&' + cl + '$4,' + A + '$A:$A,"<="&' + cl + '$5';
  };

  // Team overview
  const teamLabels = TEAMS.map(function (t) { return t.label; });
  const overviewStart = 7;
  let row = writeTable_(sh, overviewStart, 'TEAM OVERVIEW (total output)', 'Team', teamLabels, function (r, lb, cl) {
    return '=SUMIFS(' + A + '$G:$G,' + A + '$B:$B,$A' + r + ',' + base(cl) + ')';
  }, true);
  addBarChart_(sh, overviewStart + 1, TEAMS.length, 'Team output - Weekly / Monthly / Last Month', [3, 4, 5], 9, overviewStart);
  row = Math.max(row, overviewStart + 17);

  TEAMS.forEach(function (team) {
    const sectionStart = row;
    sh.getRange(row, 1, 1, 7).setValues([['', '', '', '', '', '', '']]);
    const people = team.people.map(function (p) { return p.name; });
    const afterPeople = writeTable_(sh, row, team.label.toUpperCase() + ' - by person', 'Person', people, function (r, lb, cl) {
      return '=SUMIFS(' + A + '$G:$G,' + A + '$B:$B,"' + team.label + '",' + A + '$C:$C,$A' + r + ',' + base(cl) + ')';
    }, true);
    addBarChart_(sh, sectionStart + 1, people.length, team.label + ' - person output (Weekly / Monthly / Last Month)', [3, 4, 5], 9, sectionStart);
    row = writeTable_(sh, afterPeople, team.label + ' - by ' + team.catLabel, team.catLabel, team.categories, function (r, lb, cl) {
      return '=SUMIFS(' + A + '$G:$G,' + A + '$B:$B,"' + team.label + '",' + A + '$D:$D,$A' + r + ',' + base(cl) + ')';
    }, true);
    row = Math.max(row, sectionStart + 17);
  });

  sh.setColumnWidth(1, 230);
  sh.setColumnWidths(2, 6, 105);
  sh.setFrozenRows(5);
  sh.setTabColor(BLUE);
}

/** Column chart of a table: labels in col A (header at headerRow), series from the given column indexes. */
function addBarChart_(sh, headerRow, n, title, cols, anchorCol, anchorRow) {
  const b = sh.newChart().setChartType(Charts.ChartType.COLUMN)
    .addRange(sh.getRange(headerRow, 1, n + 1, 1));
  cols.forEach(function (c) { b.addRange(sh.getRange(headerRow, c, n + 1, 1)); });
  b.setNumHeaders(1).setOption('title', title).setOption('legend', { position: 'bottom' })
    .setOption('width', 620).setOption('height', 300)
    .setPosition(anchorRow, anchorCol, 0, 0);
  sh.insertChart(b.build());
}

function buildYtDashboard_() {
  const sh = resetSheet_('YT Dashboard', 1);
  const ytTeam = TEAMS.filter(function (t) { return t.label === YT_TEAM; })[0];

  sh.getRange('A1').setValue('YouTube SEO - Channel-wise Dashboard').setFontSize(18).setFontWeight('bold');
  sh.getRange('A2').setValue('Select period').setFontWeight('bold');
  sh.getRange('B2').setValue('Monthly').setBackground('#fff3cd').setFontWeight('bold')
    .setDataValidation(SpreadsheetApp.newDataValidation().requireValueInList(PERIODS, true).setAllowInvalid(false).build());
  sh.getRange('A3:A4').setValues([['From'], ['To']]).setFontWeight('bold');
  sh.getRange('B3').setFormula('=INDEX(Dashboard!$B$4:$G$4,MATCH($B$2,Dashboard!$B$3:$G$3,0))');
  sh.getRange('B4').setFormula('=INDEX(Dashboard!$B$5:$G$5,MATCH($B$2,Dashboard!$B$3:$G$3,0))');
  sh.getRange('B3:B4').setNumberFormat('dd-mmm-yyyy');

  const pairs = [];
  ytTeam.people.forEach(function (p) { channelList_(p).forEach(function (ch) { pairs.push([p.name, ch]); }); });
  const A = 'All_Data!';
  const P = 'YT_Performance!';
  const dateA = A + '$A:$A,">="&$B$3,' + A + '$A:$A,"<="&$B$4';
  const dateP = P + '$A:$A,">="&$B$3,' + P + '$A:$A,"<="&$B$4';

  // Table 1: content posted
  let row = 6;
  sh.getRange(row, 1).setValue('CONTENT POSTED (selected period)').setFontWeight('bold').setFontSize(12);
  const h1 = row + 1;
  sh.getRange(h1, 1, 1, 6).setValues([['Person', 'Channel', 'Long Videos', 'Short Videos', 'Community Posts', 'Total']])
    .setBackground(BLUE).setFontColor('#fff').setFontWeight('bold');
  const t1 = pairs.map(function (pr, i) {
    const r = h1 + 1 + i;
    const f = function (cat) {
      return '=SUMIFS(' + A + '$G:$G,' + A + '$B:$B,"' + YT_TEAM + '",' + A + '$C:$C,$A' + r + ',' + A + '$F:$F,$B' + r +
        ',' + A + '$D:$D,"' + cat + '",' + dateA + ')';
    };
    return [pr[0], pr[1], f('Long Video'), f('Short Video'), f('Community Post'), '=SUM(C' + r + ':E' + r + ')'];
  });
  sh.getRange(h1 + 1, 1, t1.length, 6).setValues(t1);
  const e1 = h1 + t1.length;
  sh.getRange(e1 + 1, 1, 1, 6).setValues([['Total', '', '=SUM(C' + (h1 + 1) + ':C' + e1 + ')', '=SUM(D' + (h1 + 1) + ':D' + e1 + ')',
    '=SUM(E' + (h1 + 1) + ':E' + e1 + ')', '=SUM(F' + (h1 + 1) + ':F' + e1 + ')']]).setFontWeight('bold').setBackground('#f1f3f4');
  sh.getRange(h1 + 1, 3, t1.length + 1, 4).setHorizontalAlignment('center');

  // Table 2: performance new vs old
  row = e1 + 4;
  sh.getRange(row, 1).setValue('PERFORMANCE - NEW vs OLD VIDEOS (selected period)').setFontWeight('bold').setFontSize(12);
  const h2 = row + 1;
  sh.getRange(h2, 1, 1, 8).setValues([['Person', 'Channel', 'New: Views', 'New: Comments', 'New: Subscribers',
    'Old: Views', 'Old: Comments', 'Old: Subscribers']]).setBackground(BLUE).setFontColor('#fff').setFontWeight('bold');
  const t2 = pairs.map(function (pr, i) {
    const r = h2 + 1 + i;
    const f = function (col, age) {
      return '=SUMIFS(' + P + '$' + col + ':$' + col + ',' + P + '$B:$B,$A' + r + ',' + P + '$C:$C,$B' + r +
        ',' + P + '$E:$E,"' + age + '",' + dateP + ')';
    };
    return [pr[0], pr[1], f('F', 'New Video'), f('G', 'New Video'), f('H', 'New Video'),
      f('F', 'Old Video'), f('G', 'Old Video'), f('H', 'Old Video')];
  });
  sh.getRange(h2 + 1, 1, t2.length, 8).setValues(t2);
  const e2 = h2 + t2.length;
  const tot = ['Total', ''];
  for (let c = 3; c <= 8; c++) tot.push('=SUM(' + colLetter_(c) + (h2 + 1) + ':' + colLetter_(c) + e2 + ')');
  sh.getRange(e2 + 1, 1, 1, 8).setValues([tot]).setFontWeight('bold').setBackground('#f1f3f4');
  sh.getRange(h2 + 1, 3, t2.length + 1, 6).setNumberFormat('#,##0').setHorizontalAlignment('center');

  sh.setColumnWidths(1, 2, 110);
  sh.setColumnWidths(3, 6, 130);
  sh.setFrozenRows(4);
  sh.setTabColor('#ea4335');
}
