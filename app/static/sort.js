// Click any <th> in a table.sortable to sort that column.
// Numeric sort is enabled when the <th> has data-sort="number".
// The cell's data-value attribute is used as the sort key when present;
// otherwise textContent is used.
document.querySelectorAll("table.sortable").forEach((table) => {
  const headers = table.tHead.rows[0].cells;
  Array.from(headers).forEach((th, idx) => {
    th.addEventListener("click", () => {
      const tbody = table.tBodies[0];
      const rows = Array.from(tbody.rows);
      const numeric = th.dataset.sort === "number";
      const asc = !th.classList.contains("sort-asc");

      Array.from(headers).forEach((h) => h.classList.remove("sort-asc", "sort-desc"));
      th.classList.add(asc ? "sort-asc" : "sort-desc");

      rows.sort((a, b) => {
        let av = a.cells[idx].dataset.value ?? a.cells[idx].textContent.trim();
        let bv = b.cells[idx].dataset.value ?? b.cells[idx].textContent.trim();
        if (numeric) {
          av = parseFloat(av) || 0;
          bv = parseFloat(bv) || 0;
          return asc ? av - bv : bv - av;
        }
        return asc ? av.localeCompare(bv) : bv.localeCompare(av);
      });

      rows.forEach((r) => tbody.appendChild(r));
    });
  });
});
