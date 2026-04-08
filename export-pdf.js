const fs = require("fs");
const path = require("path");

// Read the markdown file
const md = fs.readFileSync(path.join(__dirname, "walkthrough1.md"), "utf8");

// Convert file:/// links to plain text (they won't work in PDF)
const cleanedMd = md
  .replace(/\[([^\]]+)\]\(file:\/\/\/[^)]+\)/g, "**$1**")
  .replace(/> \[!NOTE\]\n> /g, "📝 **Note:** ")
  .replace(/> \[!TIP\]\n> /g, "💡 **Tip:** ")
  .replace(/> \[!IMPORTANT\]\n> /g, "⚠️ **Important:** ")
  .replace(/> \[!WARNING\]\n> /g, "⚠️ **Warning:** ")
  .replace(/> \[!CAUTION\]\n> /g, "🔴 **Caution:** ");

// Escape for embedding in HTML
const escapedMd = JSON.stringify(cleanedMd);

const html = `<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>TallyBridge — Complete Code Explainer</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    color: #1a1a2e;
    background: #fff;
    line-height: 1.7;
    font-size: 11pt;
    padding: 40px 50px;
    max-width: 900px;
    margin: 0 auto;
  }

  h1 {
    font-size: 28pt;
    font-weight: 700;
    color: #1a1a2e;
    margin: 0 0 8px 0;
    letter-spacing: -0.5px;
    border-bottom: 3px solid #1a1a2e;
    padding-bottom: 12px;
  }

  h2 {
    font-size: 18pt;
    font-weight: 600;
    color: #1a1a2e;
    margin: 36px 0 16px 0;
    padding-bottom: 8px;
    border-bottom: 2px solid #e5e7eb;
    page-break-after: avoid;
  }

  h3 {
    font-size: 13pt;
    font-weight: 600;
    color: #374151;
    margin: 24px 0 10px 0;
    page-break-after: avoid;
  }

  h4 {
    font-size: 11pt;
    font-weight: 600;
    color: #4b5563;
    margin: 18px 0 8px 0;
    page-break-after: avoid;
  }

  p { margin: 0 0 10px 0; }

  blockquote {
    background: #f0f4ff;
    border-left: 4px solid #1a1a2e;
    padding: 14px 18px;
    margin: 16px 0;
    border-radius: 0 8px 8px 0;
    font-size: 10.5pt;
  }

  blockquote strong { color: #1a1a2e; }

  code {
    font-family: 'JetBrains Mono', 'Consolas', 'Courier New', monospace;
    background: #f3f4f6;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 9pt;
    color: #c7254e;
  }

  pre {
    background: #1a1a2e;
    color: #e5e7eb;
    padding: 16px 20px;
    border-radius: 10px;
    overflow-x: auto;
    margin: 14px 0;
    font-size: 8.5pt;
    line-height: 1.6;
    page-break-inside: avoid;
  }

  pre code {
    background: none;
    color: inherit;
    padding: 0;
    font-size: 8.5pt;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    margin: 14px 0;
    font-size: 9.5pt;
    page-break-inside: avoid;
  }

  th {
    background: #1a1a2e;
    color: #fff;
    text-align: left;
    padding: 10px 14px;
    font-weight: 500;
    font-size: 9pt;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  td {
    padding: 9px 14px;
    border-bottom: 1px solid #e5e7eb;
    vertical-align: top;
  }

  tr:nth-child(even) td { background: #f9fafb; }

  ul, ol {
    margin: 8px 0 8px 24px;
  }

  li {
    margin: 4px 0;
  }

  hr {
    border: none;
    border-top: 2px solid #e5e7eb;
    margin: 32px 0;
  }

  strong { font-weight: 600; }

  .mermaid {
    text-align: center;
    margin: 20px 0;
    page-break-inside: avoid;
  }

  .mermaid svg {
    max-width: 100%;
    height: auto;
  }

  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }

  /* Print styles */
  @media print {
    body { padding: 20px 30px; font-size: 10pt; }
    h1 { font-size: 22pt; }
    h2 { font-size: 15pt; margin-top: 24px; }
    h3 { font-size: 12pt; }
    pre { font-size: 8pt; }
    table { font-size: 8.5pt; }
    th { padding: 7px 10px; }
    td { padding: 6px 10px; }
    .mermaid svg { max-width: 100%; }
  }

  /* PDF loading state */
  #loading {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 999;
    font-size: 18pt;
    color: #6b7280;
  }

  #loading.done { display: none; }

  #pdf-btn {
    position: fixed;
    top: 20px;
    right: 20px;
    z-index: 1000;
    background: #1a1a2e;
    color: #fff;
    border: none;
    padding: 12px 28px;
    border-radius: 10px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    font-family: 'Inter', sans-serif;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    transition: all 0.2s;
  }

  #pdf-btn:hover { background: #2d2d4e; transform: translateY(-1px); }

  @media print {
    #pdf-btn { display: none; }
  }
</style>
</head>
<body>

<div id="loading">Rendering diagrams...</div>
<button id="pdf-btn" onclick="window.print()">📄 Save as PDF</button>

<div id="content"></div>

<script>
// Configure marked
marked.setOptions({
  gfm: true,
  breaks: false,
});

// Custom renderer to handle mermaid blocks
const renderer = new marked.Renderer();
const originalCode = renderer.code;

renderer.code = function(code, language) {
  // Handle both the object form and string form
  let codeText, codeLang;
  if (typeof code === 'object' && code !== null) {
    codeText = code.text || '';
    codeLang = code.lang || language || '';
  } else {
    codeText = code || '';
    codeLang = language || '';
  }

  if (codeLang === 'mermaid') {
    return '<div class="mermaid">' + codeText + '</div>';
  }
  // Use default rendering for other code blocks
  const escaped = codeText
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  return '<pre><code class="language-' + codeLang + '">' + escaped + '</code></pre>';
};

marked.setOptions({ renderer });

// Parse markdown
const mdContent = ${escapedMd};
document.getElementById('content').innerHTML = marked.parse(mdContent);

// Initialize mermaid
mermaid.initialize({
  startOnLoad: false,
  theme: 'default',
  themeVariables: {
    primaryColor: '#e8ecf4',
    primaryBorderColor: '#1a1a2e',
    primaryTextColor: '#1a1a2e',
    lineColor: '#6b7280',
    secondaryColor: '#f0f4ff',
    tertiaryColor: '#fef3c7',
    fontFamily: 'Inter, sans-serif',
    fontSize: '13px',
  },
  sequence: {
    diagramMarginX: 20,
    diagramMarginY: 20,
    actorMargin: 60,
    width: 130,
    height: 50,
    mirrorActors: false,
    useMaxWidth: true,
  },
  er: {
    useMaxWidth: true,
    fontSize: 12,
  },
  flowchart: {
    useMaxWidth: true,
    htmlLabels: true,
    curve: 'basis',
  },
});

// Render mermaid diagrams
async function renderMermaid() {
  const elements = document.querySelectorAll('.mermaid');
  for (let i = 0; i < elements.length; i++) {
    const el = elements[i];
    const code = el.textContent.trim();
    try {
      const { svg } = await mermaid.render('mermaid-' + i, code);
      el.innerHTML = svg;
    } catch (e) {
      console.error('Mermaid error:', e);
      el.innerHTML = '<pre style="color:red">Diagram render error: ' + e.message + '</pre>';
    }
  }
  document.getElementById('loading').classList.add('done');
}

renderMermaid();
</script>
</body>
</html>`;

// Write the HTML file
const outputPath = path.join(__dirname, "TallyBridge_Walkthrough.html");
fs.writeFileSync(outputPath, html, "utf8");
console.log("HTML file created at: " + outputPath);
console.log("Open this file in your browser, then click 'Save as PDF' button (or Ctrl+P).");
