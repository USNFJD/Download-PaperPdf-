const state = {
  searchJobId: null,
  activeJobId: null,
  timer: null,
  keywords: [],
  searchPapers: [],
  currentPapers: [],
  renderSourcePapers: [],
  repoKeys: new Set(),
  repoMembership: new Map(),
  repoSequences: new Map(),
  mode: "search",
  sortMode: "relevance",
  sortDirections: { relevance: "desc", date: "desc" },
  pdfFilter: "all",
  journalFilter: "all",
  quartileFilter: "all",
  sortPickerField: "relevance",
  pickerPaperKey: "",
  pickerAction: "add",
  previewOpen: false,
  currentProject: "",
  currentProjectPath: "",
  hasProject: false,
  clientEventCursor: 0,
};

const $ = (id) => document.getElementById(id);

const SUB_CHARS = {
  0x2080: "0", 0x2081: "1", 0x2082: "2", 0x2083: "3", 0x2084: "4",
  0x2085: "5", 0x2086: "6", 0x2087: "7", 0x2088: "8", 0x2089: "9",
  0x208a: "+", 0x208b: "-", 0x208c: "=", 0x208d: "(", 0x208e: ")",
  0x2090: "a", 0x2091: "e", 0x2095: "h", 0x1d62: "i", 0x2c7c: "j",
  0x2096: "k", 0x2097: "l", 0x2098: "m", 0x2099: "n", 0x2092: "o",
  0x209a: "p", 0x1d63: "r", 0x209b: "s", 0x209c: "t", 0x1d64: "u",
  0x1d65: "v", 0x2093: "x",
};

const SUP_CHARS = {
  0x2070: "0", 0x00b9: "1", 0x00b2: "2", 0x00b3: "3", 0x2074: "4",
  0x2075: "5", 0x2076: "6", 0x2077: "7", 0x2078: "8", 0x2079: "9",
  0x207a: "+", 0x207b: "-", 0x207c: "=", 0x207d: "(", 0x207e: ")",
  0x1d2c: "A", 0x1d2e: "B", 0x1d30: "D", 0x1d31: "E", 0x1d33: "G",
  0x1d34: "H", 0x1d35: "I", 0x1d36: "J", 0x1d37: "K", 0x1d38: "L",
  0x1d39: "M", 0x1d3a: "N", 0x1d3c: "O", 0x1d3e: "P", 0x1d3f: "R",
  0x1d40: "T", 0x1d41: "U", 0x2c7d: "V", 0x1d42: "W", 0x1d43: "a",
  0x1d47: "b", 0x1d9c: "c", 0x1d48: "d", 0x1d49: "e", 0x1da0: "f",
  0x1d4d: "g", 0x02b0: "h", 0x2071: "i", 0x02b2: "j", 0x1d4f: "k",
  0x02e1: "l", 0x1d50: "m", 0x207f: "n", 0x1d52: "o", 0x1d56: "p",
  0x02b3: "r", 0x02e2: "s", 0x1d57: "t", 0x1d58: "u", 0x1d5b: "v",
  0x02b7: "w", 0x02e3: "x", 0x02b8: "y", 0x1dbb: "z",
};

function readKeywords() {
  const seen = new Set();
  const values = [];
  for (let i = 1; i <= 3; i += 1) {
    const value = $(`kw${i}`).value.trim();
    const key = value.toLowerCase();
    if (value && !seen.has(key)) {
      seen.add(key);
      values.push(value);
    }
  }
  return values;
}

function readTitleQuery() {
  return $("titleQuery").value.trim();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function paperKey(paper) {
  return String(paper.doi || paper.id || paper.pdf_url || "");
}

function normalizedPaperKey(paper) {
  return paperKey(paper).toLowerCase();
}

function pubTime(paper) {
  const value = Date.parse(paper.publication_date || "");
  return Number.isFinite(value) ? value : 0;
}

function relevanceValue(paper) {
  return Number(paper.relevance_score || 0);
}

function relevancePercent(paper, papers) {
  const max = Math.max(...papers.map(relevanceValue), 0);
  if (!max) return 0;
  return Math.max(0, Math.min(100, Math.round((relevanceValue(paper) / max) * 100)));
}

function pdfPreviewUrl(paper) {
  const paperId = (paper.id || "").split("/").filter(Boolean).pop();
  if (paper.pdf_path && paperId) return `/api/pdf-preview/${encodeURIComponent(paperId)}`;
  return "";
}

function repoSequenceForPaper(paper, renderedIndex, mode) {
  if (mode === "repo") return String(renderedIndex + 1);
  const key = normalizedPaperKey(paper);
  const selectedRepoId = $("repoSelect").value;
  return state.repoSequences.get(`${selectedRepoId}:${key}`) || state.repoSequences.get(key) || String(renderedIndex + 1);
}

function pdfCellHtml(paper, paperKeyValue, previewTitle) {
  const actions = [];
  const previewUrl = pdfPreviewUrl(paper);
  if (previewUrl) {
    actions.push(
      `<button class="mini preview-btn" type="button" data-preview-url="${escapeAttr(previewUrl)}" data-preview-title="${escapeAttr(previewTitle)}" data-paper-key="${paperKeyValue}">预览</button>`,
    );
  }
  if (!previewUrl && paper.pdf_url) {
    actions.push(
      `<button class="mini source-btn" type="button" data-paper-key="${paperKeyValue}" data-preview-title="${escapeAttr(previewTitle)}" data-source-url="${escapeAttr(paper.pdf_url)}" data-home-url="${escapeAttr(paper.source_url || paper.pdf_url)}">源</button>`,
    );
  }
  if (!actions.length) {
    actions.push(`<button class="mini" type="button" disabled>无源</button>`);
  }
  return `<div class="pdf-actions">${actions.join("")}</div>`;
}

function applyPdfFilter(papers) {
  if (state.pdfFilter === "local") return papers.filter((paper) => paper.pdf_path);
  if (state.pdfFilter === "source") return papers.filter((paper) => paper.pdf_url);
  if (state.pdfFilter === "none") return papers.filter((paper) => !paper.pdf_path && !paper.pdf_url);
  return papers;
}

function applyJournalFilter(papers) {
  if (state.journalFilter === "all") return papers;
  return papers.filter((paper) => String(paper.journal || "未记录") === state.journalFilter);
}

function paperQuartile(paper) {
  const match = String(paper.sci_quartile || "").toUpperCase().match(/Q[1-4]/);
  return match ? match[0] : "";
}

function applyQuartileFilter(papers) {
  if (state.quartileFilter === "all") return papers;
  if (state.quartileFilter === "other") return papers.filter((paper) => !paperQuartile(paper));
  return papers.filter((paper) => paperQuartile(paper) === state.quartileFilter);
}

function sortPapers(papers, sortMode = state.sortMode) {
  const next = [...papers];
  if (sortMode === "date") {
    const factor = state.sortDirections.date === "asc" ? 1 : -1;
    next.sort((a, b) => (pubTime(a) - pubTime(b)) * factor || relevanceValue(b) - relevanceValue(a));
  } else {
    const factor = state.sortDirections.relevance === "asc" ? 1 : -1;
    next.sort((a, b) => (relevanceValue(a) - relevanceValue(b)) * factor || pubTime(b) - pubTime(a));
  }
  return next;
}

function normalizeSimpleMarkup(text) {
  return String(text ?? "")
    .replace(/<\/?\s*i\s*>/gi, (match) => (match.includes("/") ? "</i>" : "<i>"))
    .replace(/<\/?\s*em\s*>/gi, (match) => (match.includes("/") ? "</i>" : "<i>"))
    .replace(/<\/?\s*sub\s*>/gi, (match) => (match.includes("/") ? "</sub>" : "<sub>"))
    .replace(/<\/?\s*sup\s*>/gi, (match) => (match.includes("/") ? "</sup>" : "<sup>"));
}

function renderText(text) {
  const normalized = normalizeSimpleMarkup(text);
  let out = "";
  const tagPattern = /<\/?(?:i|sub|sup)>/gi;
  let pos = 0;
  for (const match of normalized.matchAll(tagPattern)) {
    out += renderUnicodeScripts(normalized.slice(pos, match.index));
    out += match[0].toLowerCase();
    pos = match.index + match[0].length;
  }
  out += renderUnicodeScripts(normalized.slice(pos));
  return out;
}

function renderUnicodeScripts(text) {
  let out = "";
  let buffer = "";
  let mode = "normal";
  const flush = () => {
    if (!buffer) return;
    const escaped = escapeHtml(buffer);
    if (mode === "sub") out += `<sub>${escaped}</sub>`;
    else if (mode === "sup") out += `<sup>${escaped}</sup>`;
    else out += escaped;
    buffer = "";
  };

  for (const char of String(text ?? "")) {
    const code = char.codePointAt(0);
    const nextMode = SUB_CHARS[code] ? "sub" : SUP_CHARS[code] ? "sup" : "normal";
    const value = SUB_CHARS[code] || SUP_CHARS[code] || char;
    if (nextMode !== mode) {
      flush();
      mode = nextMode;
    }
    buffer += value;
  }
  flush();
  return out;
}

function highlightKeywords(value) {
  const text = String(value ?? "");
  const keywords = state.keywords.filter(Boolean).sort((a, b) => b.length - a.length);
  if (!text || !keywords.length) return renderText(text);

  const pattern = new RegExp(`(${keywords.map(escapeRegExp).join("|")})`, "gi");
  return text
    .split(pattern)
    .map((part) => {
      if (!part) return "";
      const hit = keywords.some((keyword) => keyword.toLowerCase() === part.toLowerCase());
      const rendered = renderText(part);
      return hit ? `<mark class="keyword-hit">${rendered}</mark>` : rendered;
    })
    .join("");
}

function clippedText(value, limit = 900) {
  const text = String(value ?? "");
  if (text.length <= limit) return text;
  return `${text.slice(0, limit).trim()}...`;
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const body = options.body || null;
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open(method, path, true);
    request.setRequestHeader("Content-Type", "application/json");
    request.onload = () => {
      if (request.status < 200 || request.status >= 300) {
        reject(new Error(request.responseText || request.statusText));
        return;
      }
      try {
        resolve(request.responseText ? JSON.parse(request.responseText) : null);
      } catch (err) {
        reject(err);
      }
    };
    request.onerror = () => reject(new Error(request.statusText || "网络请求失败"));
    request.send(body);
  });
}

async function loadPublishers() {
  const initialPublishers = window.__INITIAL_PUBLISHERS__;
  const data = initialPublishers || await api("/api/publishers");
  window.__INITIAL_PUBLISHERS__ = null;
  $("publisherList").innerHTML = data
    .map(
      (item) => `
        <div class="publisher">
          <strong>${escapeHtml(item.name)}</strong>
          <span>${escapeHtml(item.focus)}</span>
          <span>${escapeHtml(item.examples)}</span>
        </div>
      `,
    )
    .join("");
}

async function loadSettings() {
  const initialSettings = window.__INITIAL_SETTINGS__;
  const data = initialSettings || await api("/api/settings");
  window.__INITIAL_SETTINGS__ = null;
  $("quartileText").textContent = `${data.quartile_issn_count + data.quartile_title_count} 条`;
  $("handoffWaitSelect").value = String(data.handoff_first_action_seconds ?? 10);
  state.hasProject = Boolean(data.has_project);
  state.currentProject = data.project_label || "";
  state.currentProjectPath = data.project_path || "";
  $("loadRepoBtn").textContent = "项目";
  $("statusText").textContent = state.hasProject ? `项目路径：${state.currentProjectPath}` : "请先新建项目";
  applyProjectGate();
}

async function saveSettings() {
  const handoffSeconds = Number($("handoffWaitSelect").value || 10);
  const data = await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({
      handoff_first_action_seconds: handoffSeconds,
    }),
  });
  $("handoffWaitSelect").value = String(data.handoff_first_action_seconds ?? handoffSeconds);
}

function setSearchButtonMode(hasRecord) {
  $("searchBtn").textContent = "关键词检索";
  $("titleSearchBtn").textContent = "文章名检索";
  $("searchBtn").dataset.mode = hasRecord ? "record" : "search";
  $("titleSearchBtn").dataset.mode = hasRecord ? "record" : "search";
  applyProjectGate();
}

function applyProjectGate() {
  const disabled = !state.hasProject;
  [
    "repoSelect",
    "handoffWaitSelect",
    "searchBtn",
    "titleSearchBtn",
    "addRepoBtn",
    "downloadRepoBtn",
    "openRepoFolderBtn",
    "reloadQuartileBtn",
    "openFolderBtn",
    "addAllRepoBtn",
    "pauseDownloadBtn",
  ].forEach((id) => {
    const element = $(id);
    if (element) element.disabled = disabled;
  });
  if (!disabled) {
    $("addRepoBtn").disabled = !state.searchJobId || !state.searchPapers.length;
  }
}

function setProgress(done, target, label = "杩涘害") {
  const safeTarget = Math.max(0, Number(target || 0));
  const safeDone = Math.max(0, Math.min(Number(done || 0), safeTarget || Number(done || 0)));
  if (label === "检索") {
    $("progressText").textContent = `检索：已找到 ${safeDone} 篇 / 最多 ${safeTarget || 0} 篇`;
    return;
  }
  $("progressText").textContent = `${label}: ${safeDone}/${safeTarget || 0}`;
}

function ensureProjectSelected() {
  if (state.hasProject) return true;
  $("statusText").textContent = "请先新建项目";
  applyProjectGate();
  alert("请先新建项目。");
  return false;
}

function membershipLabel(paper, mode) {
  if (mode === "repo") return repoLabel();
  const repos = state.repoMembership.get(normalizedPaperKey(paper)) || [];
  return repos.length ? repos.join("、") : "未入库";
}

function isPaperInAnyRepo(paper) {
  return (state.repoMembership.get(normalizedPaperKey(paper)) || []).length > 0;
}

function renderPapers(papers, mode = "search") {
  state.mode = mode;
  state.renderSourcePapers = [...papers];
  const sortedPapers = sortPapers(papers);
  state.currentPapers = applyJournalFilter(applyQuartileFilter(applyPdfFilter(sortedPapers)));
  const renderedPapers = state.currentPapers;
  $("paperRows").innerHTML = renderedPapers
    .map((paper, index) => {
      const authors = Array.isArray(paper.authors) ? paper.authors.slice(0, 6).join(", ") : "";
      const paperKeyValue = escapeAttr(normalizedPaperKey(paper));
      const previewTitle = repoSequenceForPaper(paper, index, mode);
      const pdfCell = pdfCellHtml(paper, paperKeyValue, previewTitle);
      const alreadyInRepo = mode === "search" && isPaperInAnyRepo(paper);
      const actionCell = mode === "repo"
        ? `<button class="mini danger" data-delete-paper="${paperKeyValue}">删除</button>`
        : alreadyInRepo
          ? `<button class="mini add-repo-btn" type="button" disabled>已入库</button>`
          : `<button class="mini add-repo-btn" type="button" data-add-paper="${paperKeyValue}">加入仓库</button>`;
      const relevance = relevancePercent(paper, renderedPapers);
      const relBg = `hsl(151 52% ${Math.max(32, 92 - relevance * 0.5)}%)`;
      const relFg = relevance >= 72 ? "#ffffff" : "#14532d";

      return `
        <tr>
          <td><div class="cell-scroll seq">${index + 1}</div></td>
          <td>
            <div class="cell-scroll">
              <div class="title">${highlightKeywords(paper.title || "")}</div>
              <div class="muted">${escapeHtml(paper.publisher || "")}</div>
            </div>
          </td>
          <td><div class="cell-scroll">${renderText(authors || "未记录")}</div></td>
          <td><div class="cell-scroll">${highlightKeywords(clippedText(paper.abstract || "无摘要"))}</div></td>
          <td><div class="cell-scroll">${escapeHtml(paper.publication_date || "未记录")}</div></td>
          <td>
            <div class="cell-scroll relevance-cell">
              <span class="relevance-badge" style="background:${relBg};color:${relFg}">${relevance}%</span>
            </div>
          </td>
          <td><div class="cell-scroll">${escapeHtml(membershipLabel(paper, mode))}</div></td>
          <td><div class="cell-scroll"><span class="quartile">${escapeHtml(paper.sci_quartile || "未配置")}</span></div></td>
          <td><div class="cell-scroll">${highlightKeywords(paper.journal || "未记录")}</div></td>
          <td><div class="cell-scroll">${pdfCell}</div></td>
          <td><div class="cell-scroll action-cell">${actionCell}</div></td>
        </tr>
      `;
    })
    .join("");

  document.querySelectorAll("[data-delete-paper]").forEach((button) => {
    button.addEventListener("click", () => deleteRepositoryItem(button.dataset.deletePaper));
  });
  document.querySelectorAll("[data-preview-url]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openPdfPreview(button.dataset.previewUrl, button.dataset.previewTitle);
    });
  });
  document.querySelectorAll(".source-btn").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      handleSourceClick(button.dataset.paperKey, button.dataset.sourceUrl, button.dataset.previewTitle, button.dataset.homeUrl).catch((err) => alert(err.message));
    });
  });
  document.querySelectorAll("[data-add-paper]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openRepoPicker(button.dataset.addPaper, button, "add");
    });
  });
}

async function openPdfPreview(url, title = "") {
  if (!url) return;
  state.previewOpen = true;
  if (window.pywebview?.api?.preview_pdf) {
    window.pywebview.api.preview_pdf(url, title).catch(() => {
      state.previewOpen = false;
    });
  } else {
    $("statusText").textContent = "当前窗口不能打开副窗口预览。";
  }
}

function closePdfPreview() {
  if (!state.previewOpen) return;
  state.previewOpen = false;
  if (window.pywebview?.api?.close_preview) {
    window.pywebview.api.close_preview().catch(() => {});
  }
}

function openRepoPicker(paperKeyValue, anchor, action = "add") {
  const picker = $("repoPicker");
  state.pickerPaperKey = paperKeyValue;
  state.pickerAction = action;
  const title = picker.querySelector(".repo-picker-title");
  if (title) {
    title.textContent = action === "manual"
      ? "保存到仓库"
      : action === "add-all"
        ? "全部加入到仓库"
        : "加入到仓库";
  }
  picker.hidden = false;
  const rect = anchor.getBoundingClientRect();
  const width = 168;
  const left = Math.max(12, Math.min(window.innerWidth - width - 12, rect.right - width));
  const top = Math.max(12, Math.min(window.innerHeight - 132, rect.bottom + 6));
  picker.style.left = `${left}px`;
  picker.style.top = `${top}px`;
}

function closeRepoPicker() {
  const picker = $("repoPicker");
  if (picker) picker.hidden = true;
  state.pickerPaperKey = "";
  state.pickerAction = "add";
}

function openPdfFilterPicker(anchor) {
  const picker = $("pdfFilterPicker");
  const papers = state.renderSourcePapers.length ? state.renderSourcePapers : state.searchPapers;
  const counts = {
    local: papers.filter((paper) => paper.pdf_path).length,
    source: papers.filter((paper) => paper.pdf_url).length,
    none: papers.filter((paper) => !paper.pdf_path && !paper.pdf_url).length,
    all: papers.length,
  };
  picker.querySelectorAll("[data-pdf-filter]").forEach((button) => {
    const labels = { local: "有本地文件", source: "有源", none: "无源", all: "显示全部" };
    const key = button.dataset.pdfFilter;
    button.textContent = `${labels[key]} (${counts[key] || 0})`;
  });
  picker.hidden = false;
  const rect = anchor.getBoundingClientRect();
  const width = 168;
  const left = Math.max(12, Math.min(window.innerWidth - width - 12, rect.right - width));
  const top = Math.max(12, Math.min(window.innerHeight - 172, rect.bottom + 6));
  picker.style.left = `${left}px`;
  picker.style.top = `${top}px`;
}

function openJournalFilterPicker(anchor) {
  const picker = $("journalFilterPicker");
  const host = $("journalFilterOptions");
  const papers = applyQuartileFilter(applyPdfFilter(state.renderSourcePapers.length ? state.renderSourcePapers : state.searchPapers));
  const counts = new Map();
  papers.forEach((paper) => {
    const journal = String(paper.journal || "未记录");
    counts.set(journal, (counts.get(journal) || 0) + 1);
  });
  const options = [["all", `显示全部 (${papers.length})`], ...Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([journal, count]) => [journal, `${journal} (${count})`])];
  host.innerHTML = options
    .map(([value, label]) => `<button type="button" data-journal-filter="${escapeAttr(value)}">${escapeHtml(label)}</button>`)
    .join("");
  host.querySelectorAll("[data-journal-filter]").forEach((button) => {
    button.addEventListener("click", () => setJournalFilter(button.dataset.journalFilter));
  });
  picker.hidden = false;
  const rect = anchor.getBoundingClientRect();
  const width = 240;
  const left = Math.max(12, Math.min(window.innerWidth - width - 12, rect.right - width));
  const top = Math.max(12, Math.min(window.innerHeight - 260, rect.bottom + 6));
  picker.style.left = `${left}px`;
  picker.style.top = `${top}px`;
}

function openQuartileFilterPicker(anchor) {
  const picker = $("quartileFilterPicker");
  const papers = applyJournalFilter(applyPdfFilter(state.renderSourcePapers.length ? state.renderSourcePapers : state.searchPapers));
  const counts = { Q1: 0, Q2: 0, Q3: 0, Q4: 0, other: 0, all: papers.length };
  papers.forEach((paper) => {
    const quartile = paperQuartile(paper);
    if (quartile && Object.prototype.hasOwnProperty.call(counts, quartile)) {
      counts[quartile] += 1;
    } else {
      counts.other += 1;
    }
  });
  picker.querySelectorAll("[data-quartile-filter]").forEach((button) => {
    const key = button.dataset.quartileFilter;
    button.textContent = key === "all" ? `显示全部 (${counts.all})` : `${key === "other" ? "其他" : key} (${counts[key] || 0})`;
  });
  picker.hidden = false;
  const rect = anchor.getBoundingClientRect();
  const width = 168;
  const left = Math.max(12, Math.min(window.innerWidth - width - 12, rect.right - width));
  const top = Math.max(12, Math.min(window.innerHeight - 204, rect.bottom + 6));
  picker.style.left = `${left}px`;
  picker.style.top = `${top}px`;
}

function closeJournalFilterPicker() {
  const picker = $("journalFilterPicker");
  if (picker) picker.hidden = true;
}

function closeQuartileFilterPicker() {
  const picker = $("quartileFilterPicker");
  if (picker) picker.hidden = true;
}

function closePdfFilterPicker() {
  const picker = $("pdfFilterPicker");
  if (picker) picker.hidden = true;
}

function setPdfFilter(filter) {
  state.pdfFilter = ["local", "source", "none", "all"].includes(filter) ? filter : "all";
  closePdfFilterPicker();
  renderPapers(state.mode === "repo" ? state.renderSourcePapers : state.searchPapers, state.mode);
}

function setJournalFilter(filter) {
  state.journalFilter = filter || "all";
  closeJournalFilterPicker();
  renderPapers(state.mode === "repo" ? state.renderSourcePapers : state.searchPapers, state.mode);
}

function setQuartileFilter(filter) {
  state.quartileFilter = ["Q1", "Q2", "Q3", "Q4", "other", "all"].includes(filter) ? filter : "all";
  closeQuartileFilterPicker();
  renderPapers(state.mode === "repo" ? state.renderSourcePapers : state.searchPapers, state.mode);
}

async function handleRepoPickerChoice(repoId) {
  if (state.pickerAction === "manual") {
    await manualPdfToRepository(repoId, state.pickerPaperKey);
    closeRepoPicker();
    return;
  }
  if (state.pickerAction === "add-all") {
    await addAllSearchToRepository(repoId);
    closeRepoPicker();
    return;
  }
  await addOneToRepository(repoId);
}

async function addOneToRepository(repoId, paperKeyValue = state.pickerPaperKey) {
  if (!state.searchJobId) {
    alert("请先检索，再把文献加入仓库。");
    return;
  }
  if (!paperKeyValue) return;
  const result = await api(`/api/repositories/${repoId}/add`, {
    method: "POST",
    body: JSON.stringify({ search_job_id: state.searchJobId, paper_ids: [paperKeyValue] }),
  });
  closeRepoPicker();
  await refreshRepositoryState();
  renderPapers(state.searchPapers, "search");
  const repoText = repoName(repoId);
  $("statusText").textContent = result.added ? `已加入 ${repoText}` : `这篇已在 ${repoText} 中`;
}

async function downloadOneToSelectedRepository(paperKeyValue, showAlert = true) {
  const repoId = $("repoSelect").value;
  if (repoId === "search") {
    const message = "请先在仓库下拉框选择仓库 1、2 或 3，再下载到仓库。";
    if (showAlert) alert(message);
    throw new Error(message);
  }
  if (!paperKeyValue) return;
  $("statusText").textContent = `${repoLabel()} 单篇下载中`;
  const data = await api(`/api/repositories/${repoId}/download-one`, {
    method: "POST",
    body: JSON.stringify({
      search_job_id: state.searchJobId || "",
      paper_id: paperKeyValue,
    }),
  });
  state.activeJobId = data.job_id;
  startPolling();
}

async function handleSourceClick(paperKeyValue, sourceUrl, previewTitle = "", homeUrl = "") {
  if (!paperKeyValue) return;
  if (!sourceUrl) return;
  await openSourcePreview(sourceUrl, paperKeyValue, previewTitle, homeUrl);
}

async function openSourcePreview(sourceUrl, paperKeyValue, previewTitle = "", homeUrl = "") {
  state.previewOpen = true;
  const repoId = $("repoSelect").value;
  if (window.pywebview?.api?.source_pdf) {
    window.pywebview.api.source_pdf(
      sourceUrl,
      paperKeyValue,
      state.searchJobId || "",
      repoId,
      previewTitle,
      homeUrl || sourceUrl,
    ).catch(() => {
      state.previewOpen = false;
      $("statusText").textContent = "当前窗口不能打开源副窗口。";
    });
  } else {
    $("statusText").textContent = "当前窗口不能打开源副窗口。";
  }
}

async function manualPdfToRepository(repoId, paperKeyValue) {
  if (!repoId || repoId === "search") return;
  const previousRepoId = $("repoSelect").value;
  $("repoSelect").value = repoId;
  try {
    await manualPdfToSelectedRepository(paperKeyValue);
  } finally {
    $("repoSelect").value = previousRepoId;
  }
}

async function manualPdfToSelectedRepository(paperKeyValue) {
  const repoId = $("repoSelect").value;
  if (repoId === "search") {
    const message = "请先在仓库下拉框选择仓库 1、2 或 3，再打开源 PDF 下载。";
    alert(message);
    throw new Error(message);
  }
  if (!paperKeyValue) return;
  $("statusText").textContent = `${repoLabel()} 单篇源 PDF 下载中`;
  const data = await api(`/api/repositories/${repoId}/manual-pdf`, {
    method: "POST",
    body: JSON.stringify({
      search_job_id: state.searchJobId || "",
      paper_id: paperKeyValue,
    }),
  });
  state.activeJobId = data.job_id;
  startPolling();
}

function sortCurrentSearch(sortMode) {
  state.sortMode = sortMode;
  renderPapers(state.mode === "repo" ? state.renderSourcePapers : state.searchPapers, state.mode);
  const direction = state.sortDirections[sortMode] === "asc" ? "正序" : "倒序";
  $("statusText").textContent = `${sortMode === "date" ? "发表时间" : "关联度"}${direction}`;
}

function openSortPicker(field, anchor) {
  state.sortPickerField = field;
  $("sortPickerTitle").textContent = field === "date" ? "发表时间排序" : "关联度排序";
  const picker = $("sortPicker");
  picker.hidden = false;
  const rect = anchor.getBoundingClientRect();
  const width = 168;
  const left = Math.max(12, Math.min(window.innerWidth - width - 12, rect.right - width));
  const top = Math.max(12, Math.min(window.innerHeight - 116, rect.bottom + 6));
  picker.style.left = `${left}px`;
  picker.style.top = `${top}px`;
}

function closeSortPicker() {
  const picker = $("sortPicker");
  if (picker) picker.hidden = true;
}

function applySortDirection(direction) {
  const field = state.sortPickerField;
  state.sortDirections[field] = direction === "asc" ? "asc" : "desc";
  closeSortPicker();
  sortCurrentSearch(field);
}

function restoreSearchRecord() {
  $("repoSelect").value = "search";
  renderPapers(state.searchPapers, "search");
  $("candidateText").textContent = state.searchPapers.length;
  $("statusText").textContent = state.searchPapers.length ? "检索记录已加载" : "待检索";
  $("addRepoBtn").disabled = !state.searchJobId || !state.searchPapers.length;
}

async function loadSearchResults() {
  if (!state.searchJobId) return [];
  const papers = await api(`/api/search-results/${state.searchJobId}`);
  state.searchPapers = papers;
  await refreshRepositoryState();
  restoreSearchRecord();
  setSearchButtonMode(true);
  setProgress(papers.length, Number($("maxPapers").value || papers.length), "检索");
  return papers;
}

async function startSearch(mode = "keywords") {
  if (!ensureProjectSelected()) return;
  const keywords = readKeywords();
  const title = readTitleQuery();
  if (mode === "keywords" && !keywords.length) {
    alert("请至少输入 1 个关键词。");
    return;
  }
  if (mode === "title" && !title) {
    alert("请输入文章名。");
    return;
  }
  state.keywords = mode === "keywords" ? keywords : title.split(/\s+/).filter(Boolean);
  $("searchBtn").disabled = true;
  $("titleSearchBtn").disabled = true;
  $("addRepoBtn").disabled = true;
  $("statusText").textContent = "检索中";
  $("candidateText").textContent = "0";
  $("downloadedText").textContent = "0";
  $("failedText").textContent = "0";
  $("logs").textContent = "";
  $("paperRows").innerHTML = "";
  setProgress(0, Number($("maxPapers").value || 100), "检索");

  const payload = {
    keywords: mode === "keywords" ? keywords : [],
    title: mode === "title" ? title : "",
    mode,
    max_papers: Number($("maxPapers").value || 100),
  };
  const data = await api("/api/search", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.searchJobId = data.job_id;
  state.activeJobId = data.job_id;
  startPolling();
}

function clearSearchRecord() {
  state.searchJobId = null;
  state.searchPapers = [];
  state.keywords = [];
  $("paperRows").innerHTML = "";
  $("candidateText").textContent = "0";
  $("statusText").textContent = "检索记录已清除";
  $("addRepoBtn").disabled = true;
  setSearchButtonMode(false);
  setProgress(0, 0, "进度");
}

async function handleSearchButton() {
  await startSearch("keywords");
}

async function handleTitleSearchButton() {
  await startSearch("title");
}

async function addSelectedToRepository() {
  if (!state.searchJobId) {
    alert("请先检索。");
    return;
  }
  const repoId = $("repoSelect").value;
  if (repoId === "search") {
    alert("请选择仓库 1、2 或 3。");
    return;
  }
  const paperIds = Array.from(document.querySelectorAll(".paper-check:checked")).map((item) => item.value);
  if (!paperIds.length) {
    alert("请先勾选文献。");
    return;
  }
  const result = await api(`/api/repositories/${repoId}/add`, {
    method: "POST",
    body: JSON.stringify({ search_job_id: state.searchJobId, paper_ids: paperIds }),
  });
  $("statusText").textContent = `已添加 ${result.added} 篇到 ${repoLabel()}`;
  $("candidateText").textContent = state.searchPapers.length;
  await refreshRepositoryState();
  renderPapers(state.searchPapers, "search");
}

async function addAllSearchToRepository(repoId) {
  if (!state.searchJobId || !state.searchPapers.length) {
    alert("请先检索出文献记录。");
    return;
  }
  if (!repoId || repoId === "search") {
    alert("请选择仓库 1、2 或 3。");
    return;
  }
  const paperIds = applyJournalFilter(applyQuartileFilter(applyPdfFilter(sortPapers(state.searchPapers)))).map((paper) => normalizedPaperKey(paper)).filter(Boolean);
  if (!paperIds.length) {
    alert("当前筛选下没有可加入的文献。");
    return;
  }
  const result = await api(`/api/repositories/${repoId}/add`, {
    method: "POST",
    body: JSON.stringify({ search_job_id: state.searchJobId, paper_ids: paperIds }),
  });
  $("statusText").textContent = `已添加 ${result.added} 篇到 ${repoName(repoId)}，跳过 ${result.skipped} 篇`;
  $("candidateText").textContent = state.searchPapers.length;
  await refreshRepositoryState();
  renderPapers(state.searchPapers, "search");
}

async function loadSelectedRecord() {
  if (!ensureProjectSelected()) return [];
  const repoId = $("repoSelect").value;
  if (repoId === "search") {
    restoreSearchRecord();
    return state.searchPapers;
  }
  return loadRepository();
}

async function loadRepository() {
  if (!ensureProjectSelected()) return [];
  const repoId = $("repoSelect").value;
  if (repoId === "search") return restoreSearchRecord();
  const papers = await api(`/api/repositories/${repoId}`);
  renderPapers(papers, "repo");
  $("candidateText").textContent = papers.length;
  $("statusText").textContent = `${repoLabel()} 已加载`;
  $("addRepoBtn").disabled = !state.searchJobId || !state.searchPapers.length;
  return papers;
}

async function refreshRepositoryState() {
  if (!state.hasProject) {
    state.repoMembership = new Map();
    state.repoSequences = new Map();
    state.repoKeys = new Set();
    return;
  }
  const initialSummary = window.__INITIAL_REPOSITORY_SUMMARY__;
  const repositories = initialSummary || await api("/api/repositories/summary");
  window.__INITIAL_REPOSITORY_SUMMARY__ = null;
  const membership = new Map();
  const downloaded = new Map();
  const sequences = new Map();
  for (const [repoId, papers] of Object.entries(repositories)) {
    const label = repoName(repoId);
    for (const [index, paper] of papers.entries()) {
      const key = paper.key || normalizedPaperKey(paper);
      if (key && paper.pdf_path) downloaded.set(key, paper.pdf_path);
      if (key) {
        const sequence = String(index + 1);
        sequences.set(`${repoId}:${key}`, sequence);
        if (!sequences.has(key)) sequences.set(key, sequence);
      }
      const current = membership.get(key) || [];
      current.push(label);
      membership.set(key, current);
    }
  }
  state.repoMembership = membership;
  state.repoSequences = sequences;
  applyDownloadedPaths(downloaded, state.searchPapers);
  applyDownloadedPaths(downloaded, state.currentPapers);

  const repoId = $("repoSelect").value;
  if (repoId === "search") {
    state.repoKeys = new Set();
    return;
  }
  state.repoKeys = new Set((repositories[repoId] || []).map((paper) => normalizedPaperKey(paper)));
}

function applyDownloadedPaths(downloaded, papers) {
  for (const paper of papers || []) {
    const path = downloaded.get(normalizedPaperKey(paper));
    paper.pdf_path = path || "";
  }
}

function repoName(repoId) {
  if (repoId === "repo1") return "仓库1";
  if (repoId === "repo2") return "仓库2";
  if (repoId === "repo3") return "仓库3";
  return "检索记录";
}

async function handleRepoSelectionChange() {
  await refreshRepositoryState();
  await loadSelectedRecord();
}

async function deleteRepositoryItem(paperKeyValue) {
  const repoId = $("repoSelect").value;
  if (repoId === "search") return;
  await api(`/api/repositories/${repoId}/items?paper_id=${encodeURIComponent(paperKeyValue)}`, { method: "DELETE" });
  await refreshRepositoryState();
  await loadRepository();
}

async function handleExternalPdfImport(message = "PDF已导入") {
  await refreshRepositoryState();
  renderPapers(state.mode === "repo" ? state.renderSourcePapers : state.searchPapers, state.mode);
  $("statusText").textContent = message;
}

function handlePreviewStatus(message) {
  const value = String(message || "").trim();
  if (value) $("statusText").textContent = value;
}

async function downloadRepository() {
  if (!ensureProjectSelected()) return;
  const repoId = $("repoSelect").value;
  if (repoId === "search") {
    alert("请选择仓库 1、2 或 3 后再下载。");
    return;
  }
  $("downloadRepoBtn").disabled = true;
  $("searchBtn").disabled = true;
  $("titleSearchBtn").disabled = true;
  $("statusText").textContent = "下载仓库中";
  const data = await api(`/api/repositories/${repoId}/download`, { method: "POST" });
  state.activeJobId = data.job_id;
  startPolling();
}

async function openRepositoryDownloads() {
  if (!ensureProjectSelected()) return;
  const repoId = $("repoSelect").value;
  if (repoId === "search") {
    alert("请选择仓库 1、2 或 3。");
    return;
  }
  await api(`/api/repositories/${repoId}/open-downloads`, { method: "POST" });
}

function openProjectPicker(anchor) {
  const picker = $("projectPicker");
  $("projectPickerTitle").textContent = state.currentProject ? `项目：${state.currentProject}` : "项目";
  picker.hidden = false;
  const rect = anchor.getBoundingClientRect();
  const width = 190;
  const left = Math.max(12, Math.min(window.innerWidth - width - 12, rect.right - width));
  const top = Math.max(12, Math.min(window.innerHeight - 112, rect.bottom + 6));
  picker.style.left = `${left}px`;
  picker.style.top = `${top}px`;
}

function closeProjectPicker() {
  const picker = $("projectPicker");
  if (picker) picker.hidden = true;
}

function resetProjectView() {
  if (state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
  state.searchJobId = null;
  state.activeJobId = null;
  state.searchPapers = [];
  state.currentPapers = [];
  state.repoKeys = new Set();
  state.repoMembership = new Map();
  state.repoSequences = new Map();
  state.mode = "search";
  $("repoSelect").value = "search";
  $("paperRows").innerHTML = "";
  $("candidateText").textContent = "0";
  $("downloadedText").textContent = "0";
  $("failedText").textContent = "0";
  $("logs").textContent = "";
  setSearchButtonMode(false);
  setProgress(0, 0, "进度");
}

async function loadProjects() {
  return api("/api/projects");
}

async function selectProject(projectId) {
  const result = await api("/api/projects/select", {
    method: "POST",
    body: JSON.stringify({ name: projectId }),
  });
  closeProjectPicker();
  resetProjectView();
  state.currentProject = result.name || projectId;
  await loadSettings();
  await refreshRepositoryState();
  restoreSearchRecord();
  applyProjectGate();
  $("statusText").textContent = `项目路径：${state.currentProjectPath}`;
}

async function createProject() {
  const name = prompt("请输入新项目文件夹名称：");
  if (!name || !name.trim()) return;
  const created = await api("/api/projects", {
    method: "POST",
    body: JSON.stringify({ name: name.trim() }),
  });
  await selectProject(created.id);
}

async function chooseProject() {
  const pyApi = window.pywebview?.api;
  if (!pyApi?.select_project_folder) {
    alert("当前窗口不能打开文件夹选择框。");
    return;
  }
  const selected = await pyApi.select_project_folder();
  if (!selected?.path) {
    return;
  }
  await selectProject(selected.path);
}

function repoLabel() {
  return $("repoSelect").options[$("repoSelect").selectedIndex].textContent;
}

function readableStatus(job) {
  if (job.kind === "search") {
    if (job.status === "ready") return `检索完成，找到 ${job.total || 0} 篇`;
    if (job.status === "searching") return "正在检索文献";
    if (job.status === "error") return "检索失败，请查看日志";
  }
  if (job.kind === "download") {
    if (job.status === "finished") return `仓库下载完成：成功 ${job.downloaded || 0} 篇，跳过 ${job.skipped || 0} 篇，失败 ${job.failed || 0} 篇`;
    if (job.status === "paused") return `仓库下载已暂停：${(job.downloaded || 0) + (job.failed || 0) + (job.skipped || 0)}/${job.total || job.target || 0}`;
    if (job.status === "downloading") return `正在下载仓库：${(job.downloaded || 0) + (job.failed || 0) + (job.skipped || 0)}/${job.total || job.target || 0}`;
    if (job.status === "error") return "仓库下载失败，请查看日志";
  }
  if (job.kind === "manual_pdf") {
    if (job.status === "finished") return job.downloaded ? "这篇 PDF 已保存到仓库" : "这篇已跳过";
    if (job.status === "manual_pdf") return "正在打开并保存这篇 PDF";
    if (job.status === "error") return "这篇下载失败，请查看日志";
  }
  return job.status || "处理中";
}

function readableLog(line) {
  let text = String(line || "");
  text = text.replace(/^\d{2}:\d{2}:\d{2}\s*/, "");
  if (/Keywords:/.test(text)) return `检索关键词：${text.split("Keywords:").pop().trim()}`;
  if (/Search ready: showing (\d+)/.test(text)) return `检索完成，显示 ${RegExp.$1} 篇候选文献。`;
  if (/Search failed|Search job error/.test(text)) return `检索遇到问题：${humanError(text)}`;
  if (/Download finished:/.test(text)) return humanError(text);
  if (/Manual PDF download error|Download job error/.test(text)) return `下载失败：${humanError(text)}`;
  return humanError(text);
}

function humanError(text) {
  const value = String(text || "");
  if (/MANUAL_HUMAN_VERIFICATION/i.test(value)) {
    const seconds = Number($("handoffWaitSelect")?.value || 10);
    const label = seconds >= 60 ? "1 分钟" : `${seconds} 秒`;
    return `页面要求真人验证，请在主窗口旁边的副窗口中操作；${label}内不操作会跳过。`;
  }
  if (/no manual action within (\d+) seconds/i.test(value)) return `${RegExp.$1} 秒内没有人工操作，已跳过这篇。`;
  if (/SKIP_HUMAN_VERIFICATION|human verification|captcha/i.test(value)) return "页面要求真人验证，正在打开主窗口旁边的副窗口。";
  if (/403|access denied/i.test(value)) return "网站拒绝自动下载，可能需要登录、权限或人工验证。";
  if (/timeout|timed out/i.test(value)) return "等待网站响应超时，稍后可重试。";
  if (/Browser did not save PDF|did not capture a PDF/i.test(value)) return "没有拿到 PDF 文件，可能该页面不是直接 PDF 或需要人工点击下载。";
  if (/Permission denied/i.test(value)) return "保存文件失败，可能文件正在被打开，或目标目录没有写入权限。";
  if (/not a PDF/i.test(value)) return "链接返回的不是 PDF 文件。";
  return value.length > 120 ? `${value.slice(0, 120)}...` : value;
}

function startPolling() {
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(refreshJob, 1500);
  refreshJob();
}

async function refreshJob() {
  if (!state.activeJobId) return;
  const job = await api(`/api/jobs/${state.activeJobId}`);
  $("statusText").textContent = readableStatus(job);
  if (job.kind === "search") {
    $("candidateText").textContent = `${job.total || job.searched || 0}`;
    setProgress(Math.min(job.searched || job.total || 0, job.target || 0), job.target || Number($("maxPapers").value || 100), "检索");
  } else if (["download", "manual_pdf"].includes(job.kind)) {
    setProgress((job.downloaded || 0) + (job.failed || 0) + (job.skipped || 0), job.target || job.total || 0, "下载");
  }
  $("downloadedText").textContent = `${job.downloaded}/${job.total || job.target || 0}`;
  $("failedText").textContent = job.failed;
  $("logs").textContent = job.logs.slice(-80).map(readableLog).join("\n");
  $("logs").scrollTop = $("logs").scrollHeight;

  if (job.kind === "search" && job.status === "ready") {
    await loadSearchResults();
    $("searchBtn").disabled = false;
    $("titleSearchBtn").disabled = false;
    applyProjectGate();
    clearInterval(state.timer);
  }

  $("pauseDownloadBtn").hidden = !(job.kind === "download" && job.status === "downloading");
  $("pauseDownloadBtn").disabled = false;

  if (["download", "manual_pdf"].includes(job.kind) && ["finished", "error", "paused"].includes(job.status)) {
    $("searchBtn").disabled = false;
    $("titleSearchBtn").disabled = false;
    $("downloadRepoBtn").disabled = false;
    applyProjectGate();
    await loadSelectedRecord();
    clearInterval(state.timer);
  }

  if (job.status === "error") {
    $("searchBtn").disabled = false;
    $("titleSearchBtn").disabled = false;
    $("downloadRepoBtn").disabled = false;
    applyProjectGate();
    clearInterval(state.timer);
  }
}

async function pauseDownload() {
  if (!state.activeJobId) return;
  $("pauseDownloadBtn").disabled = true;
  await api(`/api/jobs/${state.activeJobId}/pause`, { method: "POST" });
}

async function reloadQuartiles() {
  if (!ensureProjectSelected()) return;
  const data = await api("/api/quartiles/reload", { method: "POST" });
  $("quartileText").textContent = `${data.issn_count + data.title_count} 条`;
  await loadSelectedRecord();
}

async function openDownloads() {
  if (!ensureProjectSelected()) return;
  await api("/api/open-downloads", { method: "POST" });
}

$("searchBtn").addEventListener("click", () => handleSearchButton().catch((err) => alert(err.message)));
$("titleSearchBtn").addEventListener("click", () => handleTitleSearchButton().catch((err) => alert(err.message)));
$("addRepoBtn").addEventListener("click", () => addSelectedToRepository().catch((err) => alert(err.message)));
$("loadRepoBtn").addEventListener("click", (event) => openProjectPicker(event.currentTarget));
$("downloadRepoBtn").addEventListener("click", () => downloadRepository().catch((err) => alert(err.message)));
$("openRepoFolderBtn").addEventListener("click", () => openRepositoryDownloads().catch((err) => alert(err.message)));
$("newProjectBtn").addEventListener("click", () => createProject().catch((err) => alert(err.message)));
$("selectProjectBtn").addEventListener("click", () => chooseProject().catch((err) => alert(err.message)));
$("sortRelevanceBtn").addEventListener("click", () => sortCurrentSearch("relevance"));
$("sortDateBtn").addEventListener("click", (event) => openSortPicker("date", event.currentTarget));
$("sortRelevanceValueBtn").addEventListener("click", (event) => openSortPicker("relevance", event.currentTarget));
$("pdfFilterBtn").addEventListener("click", (event) => openPdfFilterPicker(event.currentTarget));
$("journalFilterBtn").addEventListener("click", (event) => openJournalFilterPicker(event.currentTarget));
$("quartileFilterBtn").addEventListener("click", (event) => openQuartileFilterPicker(event.currentTarget));
$("addAllRepoBtn").addEventListener("click", (event) => {
  if (!state.searchJobId || !state.searchPapers.length) {
    alert("请先检索出文献记录。");
    return;
  }
  openRepoPicker("", event.currentTarget, "add-all");
});
$("repoSelect").addEventListener("change", () => handleRepoSelectionChange().catch((err) => alert(err.message)));
$("handoffWaitSelect").addEventListener("change", () => saveSettings().catch((err) => alert(err.message)));
$("reloadQuartileBtn").addEventListener("click", () => reloadQuartiles().catch((err) => alert(err.message)));
$("openFolderBtn").addEventListener("click", () => openDownloads().catch((err) => alert(err.message)));
$("pauseDownloadBtn").addEventListener("click", () => pauseDownload().catch((err) => alert(err.message)));
document.querySelectorAll("[data-picker-repo]").forEach((button) => {
  button.addEventListener("click", () => handleRepoPickerChoice(button.dataset.pickerRepo).catch((err) => alert(err.message)));
});
document.querySelectorAll("[data-pdf-filter]").forEach((button) => {
  button.addEventListener("click", () => setPdfFilter(button.dataset.pdfFilter));
});
document.querySelectorAll("[data-quartile-filter]").forEach((button) => {
  button.addEventListener("click", () => setQuartileFilter(button.dataset.quartileFilter));
});
document.querySelectorAll("[data-sort-direction]").forEach((button) => {
  button.addEventListener("click", () => applySortDirection(button.dataset.sortDirection));
});
document.addEventListener("mousedown", (event) => {
  if (
    !$("repoPicker").hidden
    && !event.target.closest("#repoPicker")
    && !event.target.closest("[data-add-paper]")
    && !event.target.closest("#addAllRepoBtn")
  ) {
    closeRepoPicker();
  }
  if (
    !$("projectPicker").hidden
    && !event.target.closest("#projectPicker")
    && !event.target.closest("#loadRepoBtn")
  ) {
    closeProjectPicker();
  }
  if (
    !$("pdfFilterPicker").hidden
    && !event.target.closest("#pdfFilterPicker")
    && !event.target.closest("#pdfFilterBtn")
  ) {
    closePdfFilterPicker();
  }
  if (
    !$("journalFilterPicker").hidden
    && !event.target.closest("#journalFilterPicker")
    && !event.target.closest("#journalFilterBtn")
  ) {
    closeJournalFilterPicker();
  }
  if (
    !$("quartileFilterPicker").hidden
    && !event.target.closest("#quartileFilterPicker")
    && !event.target.closest("#quartileFilterBtn")
  ) {
    closeQuartileFilterPicker();
  }
  if (
    !$("sortPicker").hidden
    && !event.target.closest("#sortPicker")
    && !event.target.closest("#sortDateBtn")
    && !event.target.closest("#sortRelevanceValueBtn")
  ) {
    closeSortPicker();
  }
});

try {
  const pdfEvents = new BroadcastChannel("paper-pdf-events");
  pdfEvents.onmessage = (event) => {
    if (event.data?.type === "paper-pdf-imported") {
      handleExternalPdfImport(event.data.message || "PDF已导入").catch((err) => {
        $("statusText").textContent = err.message;
      });
    }
  };
} catch (err) {}

try {
  const statusEvents = new BroadcastChannel("paper-status-events");
  statusEvents.onmessage = (event) => {
    if (event.data?.type === "preview-status") {
      handlePreviewStatus(event.data.message);
    }
  };
} catch (err) {}

window.addEventListener("storage", (event) => {
  if (event.key === "paper-pdf-imported" && event.newValue) {
    try {
      const data = JSON.parse(event.newValue);
      handleExternalPdfImport(data.message || "PDF已导入").catch((err) => {
        $("statusText").textContent = err.message;
      });
    } catch (err) {
      handleExternalPdfImport().catch((importErr) => {
        $("statusText").textContent = importErr.message;
      });
    }
    return;
  }
  if (event.key === "paper-preview-status" && event.newValue) {
    try {
      const data = JSON.parse(event.newValue);
      handlePreviewStatus(data.message);
    } catch (err) {}
  }
});

window.addEventListener("focus", () => {
  if (!state.hasProject) return;
  refreshRepositoryState()
    .then(() => renderPapers(state.mode === "repo" ? state.renderSourcePapers : state.searchPapers, state.mode))
    .catch(() => {});
});

async function pollClientEvents() {
  try {
    const data = await api(`/api/client-events?since=${state.clientEventCursor || 0}`);
    state.clientEventCursor = Number(data.latest || state.clientEventCursor || 0);
    for (const event of data.events || []) {
      if (event.type === "paper-pdf-imported") {
        await handleExternalPdfImport(event.message || "PDF提取完成");
      }
    }
  } catch (err) {
    // Keep polling; the backend may be restarting.
  } finally {
    setTimeout(() => pollClientEvents(), 2000);
  }
}

function mountListStatusLine() {
  const main = document.querySelector(".main");
  const content = document.querySelector(".content");
  const statusText = $("statusText");
  if (!main || !content || !statusText) return;
  const statusItem = statusText.closest(".status-band > div") || statusText.parentElement;
  if (!statusItem) return;

  let statusLine = document.querySelector(".list-status-line");
  if (!statusLine) {
    statusLine = document.createElement("div");
    statusLine.className = "list-status-line";
    content.insertAdjacentElement("afterend", statusLine);
  }
  statusItem.classList.add("list-status-item");
  statusLine.appendChild(statusItem);
  const pauseButton = $("pauseDownloadBtn");
  if (pauseButton) statusLine.appendChild(pauseButton);
}

mountListStatusLine();
pollClientEvents();
setSearchButtonMode(false);
setProgress(0, 0, "进度");
async function initializeApp() {
  await loadPublishers();
  await loadSettings();
  if (!state.hasProject) {
    $("paperRows").innerHTML = "";
    $("candidateText").textContent = "0";
    $("statusText").textContent = "请先新建项目";
    applyProjectGate();
    return;
  }
  await refreshRepositoryState();
  restoreSearchRecord();
  $("statusText").textContent = `项目路径：${state.currentProjectPath}`;
}

initializeApp().catch((err) => {
  $("statusText").textContent = err.message || "初始化失败";
});

