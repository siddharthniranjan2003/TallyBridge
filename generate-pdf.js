/**
 * PDF Export Script for TallyBridge Walkthrough
 * Uses Puppeteer to render Mermaid diagrams and export to PDF
 */
const fs = require("fs");
const path = require("path");

async function main() {
  // Dynamically import puppeteer
  let puppeteer;
  try {
    puppeteer = require("puppeteer");
  } catch {
    console.log("[Setup] Installing puppeteer...");
    const { execSync } = require("child_process");
    execSync("npm install puppeteer", { cwd: __dirname, stdio: "inherit" });
    puppeteer = require("puppeteer");
  }

  console.log("[1/4] Reading walkthrough markdown...");
  const md = fs.readFileSync(path.join(__dirname, "walkthrough1.md"), "utf8");

  // Clean up file:/// links and GitHub-style alerts
  const cleanedMd = md
    .replace(/\[([^\]]+)\]\(file:\/\/\/[^)]+\)/g, "**$1**")
    .replace(/> \[!NOTE\]\n> /g, "📝 **Note:** ")
    .replace(/> \[!TIP\]\n> /g, "💡 **Tip:** ")
    .replace(/> \[!IMPORTANT\]\n> /g, "⚠️ **Important:** ")
    .replace(/> \[!WARNING\]\n> /g, "⚠️ **Warning:** ")
    .replace(/> \[!CAUTION\]\n> /g, "🔴 **Caution:** ");

  const escapedMd = JSON.stringify(cleanedMd);

  const html = `<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>TallyBridge — Complete Code Explainer</title>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js"><\/script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.0/dist/mermaid.min.js"><\/script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    color: #1a1a2e;
    background: #fff;
    line-height: 1.7;
    font-size: 10.5pt;
    padding: 30px 40px;
  }

  h1 {
    font-size: 26pt;
    font-weight: 700;
    color: #1a1a2e;
    margin: 0 0 6px 0;
    letter-spacing: -0.5px;
    border-bottom: 3px solid #1a1a2e;
    padding-bottom: 10px;
  }

  h2 {
    font-size: 16pt;
    font-weight: 600;
    color: #1a1a2e;
    margin: 28px 0 12px 0;
    padding-bottom: 6px;
    border-bottom: 2px solid #e5e7eb;
  }

  h3 {
    font-size: 12pt;
    font-weight: 600;
    color: #374151;
    margin: 20px 0 8px 0;
  }

  h4 {
    font-size: 10.5pt;
    font-weight: 600;
    color: #4b5563;
    margin: 14px 0 6px 0;
  }

  p { margin: 0 0 8px 0; }

  blockquote {
    background: #f0f4ff;
    border-left: 4px solid #1a1a2e;
    padding: 12px 16px;
    margin: 12px 0;
    border-radius: 0 8px 8px 0;
    font-size: 10pt;
  }

  blockquote strong { color: #1a1a2e; }

  code {
    font-family: 'JetBrains Mono', 'Consolas', 'Courier New', monospace;
    background: #f3f4f6;
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 8.5pt;
    color: #c7254e;
  }

  pre {
    background: #1e1e2e;
    color: #cdd6f4;
    padding: 14px 18px;
    border-radius: 8px;
    overflow-x: auto;
    margin: 10px 0;
    font-size: 8pt;
    line-height: 1.5;
  }

  pre code {
    background: none;
    color: inherit;
    padding: 0;
    font-size: 8pt;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    margin: 10px 0;
    font-size: 9pt;
  }

  th {
    background: #1a1a2e;
    color: #fff;
    text-align: left;
    padding: 8px 12px;
    font-weight: 500;
    font-size: 8.5pt;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  td {
    padding: 7px 12px;
    border-bottom: 1px solid #e5e7eb;
    vertical-align: top;
  }

  tr:nth-child(even) td { background: #f9fafb; }

  ul, ol { margin: 6px 0 6px 22px; }
  li { margin: 3px 0; }

  hr {
    border: none;
    border-top: 2px solid #e5e7eb;
    margin: 24px 0;
  }

  strong { font-weight: 600; }

  .mermaid {
    text-align: center;
    margin: 16px 0;
    background: #fafbfc;
    padding: 16px;
    border-radius: 10px;
    border: 1px solid #e5e7eb;
  }

  .mermaid svg {
    max-width: 100% !important;
    height: auto !important;
  }

  a { color: #2563eb; text-decoration: none; }
</style>
</head>
<body>
<div id="content"></div>
<script>
const renderer = new marked.Renderer();
renderer.code = function(code, language) {
  let codeText, codeLang;
  if (typeof code === 'object' && code !== null) {
    codeText = code.text || '';
    codeLang = code.lang || language || '';
  } else {
    codeText = code || '';
    codeLang = language || '';
  }
  if (codeLang === 'mermaid') {
    return '<div class="mermaid">' + codeText + '<\\/div>';
  }
  const escaped = codeText.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  return '<pre><code class="language-' + codeLang + '">' + escaped + '<\\/code><\\/pre>';
};
marked.setOptions({ renderer: renderer, gfm: true });
const mdContent = ${escapedMd};
document.getElementById('content').innerHTML = marked.parse(mdContent);

mermaid.initialize({
  startOnLoad: false,
  theme: 'default',
  themeVariables: {
    primaryColor: '#dbeafe',
    primaryBorderColor: '#1a1a2e',
    primaryTextColor: '#1a1a2e',
    lineColor: '#6b7280',
    secondaryColor: '#f0f4ff',
    tertiaryColor: '#fef3c7',
    fontFamily: 'Inter, sans-serif',
    fontSize: '12px',
  },
  sequence: { useMaxWidth: true, mirrorActors: false, width: 120, height: 45 },
  er: { useMaxWidth: true, fontSize: 11 },
  flowchart: { useMaxWidth: true, htmlLabels: true, curve: 'basis' },
});

async function renderMermaid() {
  const elements = document.querySelectorAll('.mermaid');
  for (let i = 0; i < elements.length; i++) {
    const el = elements[i];
    const code = el.textContent.trim();
    try {
      const { svg } = await mermaid.render('mermaid-' + i, code);
      el.innerHTML = svg;
    } catch (e) {
      el.innerHTML = '<p style="color:red">Diagram error: ' + e.message + '<\\/p>';
    }
  }
  window.__MERMAID_DONE__ = true;
}
renderMermaid();
<\/script>
</body>
</html>`;

  console.log("[2/4] Launching browser...");
  const browser = await puppeteer.launch({
    headless: "new",
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  });

  const page = await browser.newPage();
  await page.setViewport({ width: 1200, height: 800 });

  console.log("[3/4] Rendering page with Mermaid diagrams...");
  await page.setContent(html, { waitUntil: "networkidle0", timeout: 60000 });

  // Wait for Mermaid diagrams to finish rendering
  await page.waitForFunction(() => window.__MERMAID_DONE__ === true, { timeout: 30000 });
  // Extra wait to ensure SVGs are fully painted
  await new Promise(r => setTimeout(r, 3000));

  const pdfPath = path.join(__dirname, "TallyBridge_Walkthrough.pdf");
  console.log("[4/4] Generating PDF...");
  await page.pdf({
    path: pdfPath,
    format: "A4",
    printBackground: true,
    margin: { top: "20mm", bottom: "20mm", left: "15mm", right: "15mm" },
    displayHeaderFooter: true,
    headerTemplate: '<div style="font-size:8px;color:#aaa;width:100%;text-align:center;font-family:Inter,sans-serif;">TallyBridge — Complete Code Explainer</div>',
    footerTemplate: '<div style="font-size:8px;color:#aaa;width:100%;text-align:center;font-family:Inter,sans-serif;">Page <span class="pageNumber"></span> of <span class="totalPages"></span></div>',
  });

  await browser.close();
  console.log("✅ PDF saved to: " + pdfPath);
}

main().catch(e => {
  console.error("Error:", e);
  process.exit(1);
});
