const fetchForm        = document.getElementById("fetchForm");
const videoUrl         = document.getElementById("videoUrl");
const fetchBtn         = document.getElementById("fetchBtn");
const fetchBtnText     = document.getElementById("fetchBtnText");
const fetchSpinner     = document.getElementById("fetchSpinner");
const status           = document.getElementById("status");
const results          = document.getElementById("results");
const videoTitle       = document.getElementById("videoTitle");
const thumbnail        = document.getElementById("thumbnail");
const quality          = document.getElementById("quality");
const downloadBtn      = document.getElementById("downloadBtn");
const downloadBtnText  = document.getElementById("downloadBtnText");
const downloadSpinner  = document.getElementById("downloadSpinner");
const downloadMessage  = document.getElementById("downloadMessage");

const progressWrap     = document.getElementById("progressWrap");
const progressTrack    = document.getElementById("progressTrack");
const progressFill     = document.getElementById("progressFill");
const progressPercent  = document.getElementById("progressPercent");
const progressDetail   = document.getElementById("progressDetail");

const POLL_INTERVAL_MS = 500;

let currentToken = null;
let activeJob    = null;


/* ---------- UI helpers ---------- */

function setFetching(isFetching) {
    fetchBtn.disabled = isFetching;
    fetchBtnText.textContent = isFetching ? "Fetching..." : "Fetch";
    fetchSpinner.hidden = !isFetching;
}

function setDownloading(isDownloading) {
    downloadBtn.disabled = isDownloading;
    downloadBtnText.textContent = isDownloading ? "Downloading..." : "Download";
    downloadSpinner.hidden = !isDownloading;
}

function formatDuration(seconds) {
    if (!Number.isFinite(seconds)) return "";
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60).toString().padStart(2, "0");
    return `${mins}:${secs}`;
}

function formatBytes(bytes) {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let n = bytes;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

function formatEta(seconds) {
    if (!seconds || !Number.isFinite(seconds)) return "";
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60).toString().padStart(2, "0");
    return m > 0 ? `${m}m ${s}s left` : `${s}s left`;
}


/* ---------- progress bar ---------- */

function resetProgress() {
    activeJob = null;
    progressWrap.hidden = true;
    progressFill.style.width = "0%";
    progressFill.classList.remove("is_processing");
    progressPercent.textContent = "0%";
    progressDetail.textContent = "";
    progressTrack.setAttribute("aria-valuenow", "0");
}

function renderProgress(job) {
    progressWrap.hidden = false;

    const pct = Math.max(0, Math.min(100, Number(job.percent) || 0));
    progressFill.style.width = `${pct}%`;
    progressTrack.setAttribute("aria-valuenow", String(Math.round(pct)));
    progressPercent.textContent =
        pct >= 99.5 ? "100%" : `${pct.toFixed(1)}%`;

    if (job.status === "downloading") {
        const bits = [];
        if (job.total)          bits.push(`${formatBytes(job.downloaded)} / ${formatBytes(job.total)}`);
        else if (job.downloaded) bits.push(formatBytes(job.downloaded));
        if (job.speed)          bits.push(`${formatBytes(job.speed)}/s`);
        if (job.eta)            bits.push(formatEta(job.eta));
        progressDetail.textContent = bits.join(" · ");
        progressFill.classList.remove("is_processing");
    } else if (job.status === "processing") {
        progressDetail.textContent = "Merging / converting…";
        progressFill.classList.add("is_processing");
    } else if (job.status === "queued") {
        progressDetail.textContent = "Starting…";
    }
}


/* ---------- fetch link ---------- */

fetchForm.addEventListener("submit", async function (event) {
    event.preventDefault();

    const url = videoUrl.value.trim();
    if (!url) {
        status.textContent = "Paste a video link first.";
        return;
    }

    currentToken = null;
    results.hidden = true;
    downloadMessage.textContent = "";
    thumbnail.hidden = true;
    resetProgress();

    status.textContent = "Checking the link and available formats...";
    setFetching(true);

    try {
        const response = await fetch("/api/info", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url })
        });

        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Could not read this link.");

        currentToken = data.token;
        videoTitle.textContent = data.title || "Video";
        if (data.duration) {
            videoTitle.textContent += ` · ${formatDuration(data.duration)}`;
        }

        if (data.thumbnail) {
            thumbnail.src = data.thumbnail;
            thumbnail.hidden = false;
            thumbnail.onerror = () => { thumbnail.hidden = true; };
        }

        quality.replaceChildren();
        for (const item of data.formats) {
            const option = document.createElement("option");
            option.value = item.value;
            option.textContent = item.label;
            quality.appendChild(option);
        }

        results.hidden = false;
        status.textContent = `Found ${data.formats.length} available format option(s).`;
    } catch (error) {
        status.textContent = error.message || "Something went wrong while fetching the link.";
    } finally {
        setFetching(false);
    }
});


/* ---------- download with progress ---------- */

downloadBtn.addEventListener("click", async function () {
    if (!currentToken) {
        downloadMessage.textContent = "Fetch a link first.";
        return;
    }

    downloadMessage.textContent = "";
    resetProgress();
    progressWrap.hidden = false;
    renderProgress({ status: "queued", percent: 0 });
    setDownloading(true);

    let jobToken;
    try {
        const response = await fetch("/api/download", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token: currentToken, quality: quality.value })
        });

        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Download failed.");
        if (!data.job_token) throw new Error("Server did not return a job token.");

        jobToken = data.job_token;
        activeJob = jobToken;
    } catch (error) {
        progressWrap.hidden = true;
        downloadMessage.textContent = error.message || "Download failed.";
        setDownloading(false);
        return;
    }

    await pollJob(jobToken);
});


async function pollJob(jobToken) {
    while (activeJob === jobToken) {
        let job;
        try {
            const r = await fetch(`/api/progress/${jobToken}`, { cache: "no-store" });
            job = await r.json();
            if (!r.ok) throw new Error(job.error || "Progress check failed.");
        } catch (error) {
            downloadMessage.textContent = error.message || "Lost connection to server.";
            setDownloading(false);
            return;
        }

        renderProgress(job);

        if (job.status === "done") {
            progressFill.style.width = "100%";
            progressPercent.textContent = "100%";
            progressDetail.textContent = job.filename || "";

            const anchor = document.createElement("a");
            anchor.href = job.download_url;
            anchor.download = job.filename || "download";
            document.body.appendChild(anchor);
            anchor.click();
            anchor.remove();

            downloadMessage.textContent = "Your file is ready. Check your browser's downloads.";
            setDownloading(false);
            return;
        }

        if (job.status === "error") {
            progressFill.classList.remove("is_processing");
            downloadMessage.textContent = job.error || "Download failed.";
            setDownloading(false);
            return;
        }

        await new Promise(res => setTimeout(res, POLL_INTERVAL_MS));
    }
}