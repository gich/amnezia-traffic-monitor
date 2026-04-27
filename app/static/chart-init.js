// For every .window-switcher: fetch JSON from data-source-url with ?window=...,
// render a Chart.js line chart into the canvas referenced by data-target.
// Buttons inside the switcher swap windows; the .active button is loaded first.

function fmtBytes(n) {
  let f = n;
  for (const u of ["B", "KB", "MB", "GB", "TB"]) {
    if (Math.abs(f) < 1024 || u === "TB") return f.toFixed(2) + " " + u;
    f /= 1024;
  }
  return f.toFixed(2) + " TB";
}

function fmtBucketLabel(unixTs, window) {
  const d = new Date(unixTs * 1000);
  if (window === "24h" || window === "1h") {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  if (window === "7d") {
    return d.toLocaleString([], { weekday: "short", hour: "2-digit" });
  }
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

document.querySelectorAll(".window-switcher").forEach((switcher) => {
  const targetId = switcher.dataset.target;
  const sourceUrl = switcher.dataset.sourceUrl;
  const canvas = document.getElementById(targetId);
  if (!canvas) return;

  let chart = null;

  async function load(window) {
    const res = await fetch(sourceUrl + "?window=" + encodeURIComponent(window));
    if (!res.ok) {
      console.error("failed to load timeseries", res.status);
      return;
    }
    const data = await res.json();
    const labels = data.map((p) => fmtBucketLabel(p.bucket_ts, window));
    const downValues = data.map((p) => p.tx);
    const upValues = data.map((p) => p.rx);

    if (chart) chart.destroy();
    chart = new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Downloaded",
            data: downValues,
            borderColor: "#0366d6",
            backgroundColor: "rgba(3,102,214,0.1)",
            tension: 0.2,
            fill: true,
          },
          {
            label: "Uploaded",
            data: upValues,
            borderColor: "#d73a49",
            backgroundColor: "rgba(215,58,73,0.05)",
            tension: 0.2,
            fill: true,
          },
        ],
      },
      options: {
        responsive: true,
        interaction: { mode: "index", intersect: false },
        scales: {
          y: {
            beginAtZero: true,
            ticks: { callback: (v) => fmtBytes(v) },
          },
        },
        plugins: {
          tooltip: {
            callbacks: {
              label: (ctx) => ctx.dataset.label + ": " + fmtBytes(ctx.parsed.y),
            },
          },
        },
      },
    });
  }

  switcher.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      switcher.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      load(btn.dataset.window);
    });
  });

  const initial = switcher.querySelector("button.active") || switcher.querySelector("button");
  if (initial) load(initial.dataset.window);
});
