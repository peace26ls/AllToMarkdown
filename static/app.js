/* AllToMarkdown 前端交互
 * 流程：上传 -> 轮询状态(进度条) -> 完成 -> 预览(marked 渲染) + 校对清单 + 下载
 * 依赖：marked.js（CDN，缺失时预览降级为展示原始 markdown）
 */
(function () {
  "use strict";

  // ---- DOM ----
  var dropzone = document.getElementById("dropzone");
  var fileInput = document.getElementById("fileInput");
  var fileLabel = document.getElementById("fileLabel");
  var convertBtn = document.getElementById("convertBtn");
  var formError = document.getElementById("formError");
  var progress = document.getElementById("progress");
  var stepLabel = document.getElementById("stepLabel");
  var progressBar = document.getElementById("progressBar");
  var progressPct = document.getElementById("progressPct");
  var result = document.getElementById("result");
  var resultMeta = document.getElementById("resultMeta");
  var previewBody = document.getElementById("previewBody");
  var rawCode = document.getElementById("rawCode");
  var reviewList = document.getElementById("reviewList");
  var dlMd = document.getElementById("dlMd");
  var dlZip = document.getElementById("dlZip");
  var historyList = document.getElementById("historyList");
  var refreshHist = document.getElementById("refreshHist");
  var mKind = document.getElementById("mKind");
  var mBlocks = document.getElementById("mBlocks");
  var mElapsed = document.getElementById("mElapsed");
  var mOrganize = document.getElementById("mOrganize");

  // ----
  var selectedFile = null;
  var currentTaskId = null;
  var pollTimer = null;

  var KIND_LABEL = {
    docx: "Word 文档",
    pdf_text: "文字版 PDF",
    pdf_scanned: "扫描件 PDF",
    unknown: "未知",
  };

  var modeHint = document.getElementById("modeHint");

  var STEP_LABEL = {
    extracting: "正在提取内容…",
    ocr: "OCR 识别扫描件…",
    vision: "多模态识别中（按页计费）…",
    organizing: "LLM 结构化整理中…",
    rendering: "渲染 Markdown…",
    done: "完成",
    failed: "失败",
    pending: "排队中",
  };

  // 各模式的说明文案（切换模式时展示，避免用户误用高成本模式）
  var MODE_HINT = {
    standard: "标准：规则提取，最快，适合 Word 与文字版 PDF。",
    high_quality: "高质量：逐页渲染送视觉模型，按页计费，适合复杂版式与扫描件。",
    ocr: "扫描件 OCR：本地 PaddleOCR 识别无文字层 PDF，CPU 较慢（首次需加载模型）。",
  };

  // 服务端能力（OCR / 视觉模型是否可用），决定模式按钮能否点击
  function applyCapabilities(caps) {
    setSegAvailability("high_quality", caps.vision);
    setSegAvailability("ocr", caps.ocr);
  }

  function setSegAvailability(mode, info) {
    var btn = document.querySelector('.seg__btn[data-seg="' + mode + '"]');
    if (!btn || !info) return;
    btn.disabled = !info.available;
    btn.title = info.reason || "";
  }

  function loadCapabilities() {
    fetch("/api/capabilities", { headers: { "Cache-Control": "no-cache" } })
      .then(function (r) { return r.json(); })
      .then(applyCapabilities)
      .catch(function () { /* 服务不可用：保持禁用 */ });
  }

  // ---- 文件选择 ----
  function pickFile(file) {
    if (!file) return;
    var ok = /\.(docx|pdf)$/i.test(file.name);
    if (!ok) {
      selectedFile = null;
      setError("仅支持 .docx 与 .pdf 文件");
      return;
    }
    selectedFile = file;
    clearError();
    fileLabel.hidden = false;
    fileLabel.textContent = "已选择：" + (file.size / 1024).toFixed(1) + " KB · " + file.name;
    convertBtn.disabled = false;
    dropzone.classList.add("has-file");
  }

  function clearSelection() {
    selectedFile = null;
    fileLabel.hidden = true;
    fileLabel.textContent = "";
    convertBtn.disabled = true;
    dropzone.classList.remove("has-file");
    fileInput.value = "";
  }

  function setError(msg) {
    formError.textContent = msg;
    formError.hidden = false;
  }
  function clearError() {
    formError.textContent = "";
    formError.hidden = true;
  }

  dropzone.addEventListener("click", function () { fileInput.click(); });
  dropzone.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener("change", function () { pickFile(fileInput.files[0]); });
  ["dragover", "dragenter"].forEach(function (ev) {
    dropzone.addEventListener(ev, function (e) {
      e.preventDefault();
      dropzone.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    dropzone.addEventListener(ev, function (e) {
      e.preventDefault();
      dropzone.classList.remove("is-dragover");
    });
  });
  dropzone.addEventListener("drop", function (e) {
    pickFile(e.dataTransfer.files[0]);
  });

  // ---- 模式分段 ----
  var segButtons = Array.prototype.slice.call(document.querySelectorAll(".seg__btn[data-seg]"));
  var currentMode = "standard";
  segButtons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (btn.disabled) return;
      segButtons.forEach(function (b) { b.classList.remove("is-active"); });
      btn.classList.add("is-active");
      currentMode = btn.getAttribute("data-seg");
      if (modeHint) modeHint.textContent = MODE_HINT[currentMode] || "";
    });
  });

  // ---- 上传 + 转换 ----
  convertBtn.addEventListener("click", function () {
    if (!selectedFile) return;
    var organize = document.getElementById("organize").checked;
    var fd = new FormData();
    fd.append("file", selectedFile);
    fd.append("mode", currentMode);
    fd.append("organize", organize ? "true" : "false");

    // 切换到进度状态
    result.hidden = true;
    progress.hidden = false;
    setProgress(4, "正在上传…", 0);
    convertBtn.disabled = true;
    clearError();

    fetch("/api/tasks", { method: "POST", body: fd })
      .then(function (res) {
        if (!res.ok) return res.json().then(function (j) { throw new Error(j.detail || "上传失败"); });
        return res.json();
      })
      .then(function (data) {
        currentTaskId = data.task_id;
        startPolling(currentTaskId);
      })
      .catch(function (err) {
        progress.hidden = true;
        convertBtn.disabled = false;
        setError(err.message || "无法连接服务");
      });
  });

  function setProgress(pct, label, serverPct) {
    var p = Math.max(2, Math.min(100, Math.max(pct, serverPct || 0)));
    progressBar.style.width = p.toFixed(1) + "%";
    progressPct.textContent = Math.round(p) + "%";
    if (label) stepLabel.textContent = label;
  }

  function startPolling(tid) {
    if (pollTimer) clearInterval(pollTimer);
    poll();
    pollTimer = setInterval(poll, 1500);

    function poll() {
      fetch("/api/tasks/" + tid, { headers: { "Cache-Control": "no-cache" } })
        .then(function (r) { return r.json(); })
        .then(function (s) {
          var label = STEP_LABEL[s.status] || "处理中";
          setProgress(s.progress, label, s.progress);
          if (s.status === "done") { stop(); onDone(tid); }
          else if (s.status === "failed") { stop(); onFail(s.error); }
        })
        .catch(function () { /* 暂时忽略，下次轮询 */ });
    }
    function stop() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
  }

  function onFail(err) {
    progress.hidden = true;
    convertBtn.disabled = false;
    stepLabel.textContent = "失败";
    setError(err || "处理失败，请重试");
  }

  function onDone(tid) {
    progress.hidden = true;
    progress.style.display = "";
    // 展示结果
    result.hidden = false;
    convertBtn.disabled = false;
    dlMd.href = "/api/tasks/" + tid + "/markdown";
    dlZip.href = "/api/tasks/" + tid + "/archive";
    dlMd.setAttribute("download", "");
    dlZip.setAttribute("download", "");
    resultMeta.textContent = "任务 " + tid;

    loadPreview(tid);
    loadMeta(tid);
  }

  // ---- 预览 ----
  function loadPreview(tid) {
    previewBody.innerHTML = '<p class="preview__loading">正在加载…</p>';
    fetch("/api/tasks/" + tid + "/preview")
      .then(function (r) { return r.text(); })
      .then(function (md) {
        rawCode.textContent = md;
        var marked = window.marked;
        if (marked && marked.parse) {
          document.body.classList.add("markdown-ready");
          previewBody.innerHTML = marked.parse(md);
        } else {
          // marked 未加载成功：降级为 <pre>
          previewBody.innerHTML = "<pre class=\"preview__fallback\"></pre>";
          previewBody.querySelector(".preview__fallback").textContent = md;
        }
      })
      .catch(function () {
        previewBody.innerHTML = '<p class="preview__error">预览加载失败。</p>';
      });
  }

  // ---- meta + 校对清单 ----
  function loadMeta(tid) {
    fetch("/api/tasks/" + tid + "/meta")
      .then(function (r) { return r.json(); })
      .then(function (m) {
        mKind.textContent = KIND_LABEL[m.detected_kind] || (m.detected_kind || "—");
        mBlocks.textContent = String(m.blocks ?? "—");
        mElapsed.textContent = (m.elapsed != null) ? m.elapsed + " s" : "—";
        mOrganize.textContent = m.organize ? "已开启" : "未开启";

        var items = (m.review_items || []).filter(function (i) {
          return i.needs_review !== false;
        });
        reviewList.innerHTML = "";
        if (!items.length) {
          var li = document.createElement("li");
          li.className = "review__empty";
          li.textContent = "暂无低置信内容，全文可直接采用。";
          reviewList.appendChild(li);
          return;
        }
        items.forEach(function (i) {
          var li = document.createElement("li");
          li.className = "review__item";
          var tag = document.createElement("span");
          tag.className = "review__tag";
          tag.textContent = i.type || "block";
          var txt = document.createElement("span");
          txt.textContent = i.reason || "低置信度";
          var loc = document.createElement("span");
          loc.className = "review__loc";
          loc.textContent = i.page ? "· 第 " + i.page + " 页" : "";
          li.appendChild(tag); li.appendChild(txt); li.appendChild(loc);
          reviewList.appendChild(li);
        });
      })
      .catch(function () { /* 忽略 */ });
  }

  // ---- 历史 ----
  function loadHistory() {
    fetch("/api/tasks?limit=10")
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var tasks = j.tasks || [];
        historyList.innerHTML = "";
        if (!tasks.length) {
          var li = document.createElement("li");
          li.className = "history__empty";
          li.textContent = "还没有任务。";
          historyList.appendChild(li);
          return;
        }
        var STATUS = { pending: "排队", extracting: "提取中", ocr: "OCR", vision: "多模态",
                       organizing: "整理中", rendering: "渲染中", done: "完成", failed: "失败" };
        tasks.forEach(function (t) {
          var li = document.createElement("li");
          li.className = "history__item";
          var name = document.createElement("span");
          name.className = "history__name";
          name.textContent = t.source_name;
          var st = document.createElement("span");
          st.className = "history__status history__status--" + t.status;
          st.textContent = STATUS[t.status] || t.status;
          var when = document.createElement("span");
          when.className = "history__when";
          if (t.status === "done") {
            when.textContent = (t.progress || 0).toFixed(0) + "%";
            li.appendChild(name); li.appendChild(st); li.appendChild(when);
            li.style.cursor = "pointer";
            li.addEventListener("click", function () { openResult(t.id, t.source_name); });
          } else {
            when.textContent = "";
            li.appendChild(name); li.appendChild(st); li.appendChild(when);
          }
          historyList.appendChild(li);
        });
      })
      .catch(function () { /* 忽略 */ });
  }

  refreshHist.addEventListener("click", loadHistory);

  // 历史点击恢复结果视图
  function openResult(tid, sourceName) {
    result.hidden = false;
    resultMeta.textContent = sourceName + " · 任务 " + tid;
    dlMd.href = "/api/tasks/" + tid + "/markdown";
    dlZip.href = "/api/tasks/" + tid + "/archive";
    dlMd.setAttribute("download", "");
    dlZip.setAttribute("download", "");
    document.getElementById("workspace").scrollIntoView({ behavior: "smooth" });
    loadPreview(tid);
    loadMeta(tid);
  }

  // 页面加载：入场动画 + 历史
  window.addEventListener("DOMContentLoaded", function () {
    var reveals = document.querySelectorAll("[data-reveal]");
    reveals.forEach(function (el, i) {
      var delay = parseInt(el.getAttribute("data-reveal-delay") || "0", 10);
      setTimeout(function () { el.classList.add("is-revealed"); }, delay);
    });
    loadCapabilities();
    loadHistory();
  });
})();
