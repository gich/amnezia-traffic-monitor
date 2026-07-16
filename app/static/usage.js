// Usage-by-period widget. For every .usage-range block: date inputs + preset
// buttons drive two fetches — /usage (period totals) and /usage_series (per-day
// bars) — rendering the big rx/tx numbers and a per-day bar chart. All dates are
// LOCAL (browser timezone), matching how peer_daily buckets days on the server.

(function () {
  function fmtBytes(n) {
    let f = n || 0;
    for (const u of ["B", "KB", "MB", "GB", "TB"]) {
      if (Math.abs(f) < 1024 || u === "TB") return f.toFixed(2) + " " + u;
      f /= 1024;
    }
    return f.toFixed(2) + " TB";
  }

  function isoDate(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  // Returns [from, to] as local YYYY-MM-DD for a named preset.
  function presetRange(preset) {
    const today = new Date();
    if (preset === "this-month") {
      return [isoDate(new Date(today.getFullYear(), today.getMonth(), 1)), isoDate(today)];
    }
    if (preset === "prev-month") {
      const first = new Date(today.getFullYear(), today.getMonth() - 1, 1);
      const last = new Date(today.getFullYear(), today.getMonth(), 0); // day 0 = last of prev month
      return [isoDate(first), isoDate(last)];
    }
    const days = preset === "7d" ? 7 : 30;
    const from = new Date(today);
    from.setDate(from.getDate() - (days - 1));
    return [isoDate(from), isoDate(today)];
  }

  document.querySelectorAll(".usage-range").forEach((root) => {
    const usageUrl = root.dataset.usageUrl;
    const seriesUrl = root.dataset.seriesUrl;
    const canvas = root.querySelector(".usage-canvas");
    const fromInput = root.querySelector(".usage-from");
    const toInput = root.querySelector(".usage-to");
    const downEl = root.querySelector(".usage-down");
    const upEl = root.querySelector(".usage-up");
    const presetButtons = root.querySelectorAll(".usage-presets button");
    let chart = null;

    function clearPresets() {
      presetButtons.forEach((b) => b.classList.remove("active"));
    }

    async function load(from, to) {
      fromInput.value = from;
      toInput.value = to;
      const qs = "?from=" + encodeURIComponent(from) + "&to=" + encodeURIComponent(to);

      const [uRes, sRes] = await Promise.all([fetch(usageUrl + qs), fetch(seriesUrl + qs)]);

      if (uRes.ok) {
        const u = await uRes.json();
        downEl.textContent = fmtBytes(u.tx); // tx from server = client download
        upEl.textContent = fmtBytes(u.rx); // rx from server = client upload
      } else {
        downEl.textContent = "—";
        upEl.textContent = "—";
      }

      if (!sRes.ok) {
        console.error("failed to load usage series", sRes.status);
        return;
      }
      const data = await sRes.json();
      const labels = data.map((p) => p.day.slice(5)); // MM-DD

      if (chart) chart.destroy();
      chart = new Chart(canvas, {
        type: "bar",
        data: {
          labels,
          datasets: [
            { label: "Downloaded", data: data.map((p) => p.tx), backgroundColor: "#0366d6" },
            { label: "Uploaded", data: data.map((p) => p.rx), backgroundColor: "#d73a49" },
          ],
        },
        options: {
          responsive: true,
          interaction: { mode: "index", intersect: false },
          scales: { y: { beginAtZero: true, ticks: { callback: (v) => fmtBytes(v) } } },
          plugins: {
            tooltip: {
              callbacks: { label: (ctx) => ctx.dataset.label + ": " + fmtBytes(ctx.parsed.y) },
            },
          },
        },
      });
    }

    presetButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        clearPresets();
        btn.classList.add("active");
        const [from, to] = presetRange(btn.dataset.preset);
        load(from, to);
      });
    });

    root.querySelector(".usage-apply").addEventListener("click", () => {
      if (!fromInput.value || !toInput.value) return;
      clearPresets();
      load(fromInput.value, toInput.value);
    });

    const initial = root.querySelector(".usage-presets button.active") || presetButtons[0];
    const [from, to] = presetRange(initial ? initial.dataset.preset : "this-month");
    load(from, to);
  });
})();
