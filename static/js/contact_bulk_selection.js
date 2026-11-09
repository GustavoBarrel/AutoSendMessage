/*
 * Bulk selection of contacts on the contact list.
 *
 * The selection is a set of contact ids kept in sessionStorage, so it survives
 * pagination, search, filters and the browser's back button within the tab.
 * Any reload clears it: browsers do not tell a normal reload from a forced one,
 * and an old, forgotten selection is riskier than losing one on purpose.
 *
 * Without JavaScript the row checkboxes still post through their `form`
 * attribute; with it, the names are removed and only the CSV field is sent.
 */
(function () {
  "use strict";

  const root = document.querySelector("[data-bulk-selection]");
  if (!root) return;

  const storageKey = root.dataset.storageKey || "contacts.bulkSelection";
  const matchedKey = `${storageKey}.matched`;
  const filterQuery = root.dataset.filterQuery || "";
  const totalMatching = Number(root.dataset.total) || 0;
  const idsUrl = root.dataset.idsUrl;
  const scope = filterQuery ? "desta busca" : "da base";

  const rows = Array.from(root.querySelectorAll("input[data-bulk-row]"));
  const pageToggle = root.querySelector("input[data-bulk-page]");
  const bar = root.querySelector("form[data-bulk-form]");
  const hiddenIds = bar ? bar.querySelector("input[name='contact_ids']") : null;
  const submit = root.querySelector("[data-bulk-submit]");
  const countOut = root.querySelector("[data-bulk-count]");
  const clearBtn = root.querySelector("[data-bulk-clear]");
  const matchBtn = root.querySelector("[data-bulk-select-matching]");
  const errorOut = root.querySelector("[data-bulk-error]");

  const fmt = (n) => n.toLocaleString("pt-BR");
  const contacts = (n) => `${fmt(n)} ${n === 1 ? "contato" : "contatos"}`;
  const isId = (id) => Number.isInteger(id) && id > 0;

  const store = {
    read(key, fallback) {
      try {
        const raw = window.sessionStorage.getItem(key);
        return raw === null ? fallback : JSON.parse(raw);
      } catch (_err) {
        return fallback;
      }
    },
    write(key, value) {
      try {
        window.sessionStorage.setItem(key, JSON.stringify(value));
      } catch (_err) {
        // Storage full or blocked: the selection still works for this page.
      }
    },
    remove(key) {
      try {
        window.sessionStorage.removeItem(key);
      } catch (_err) {
        // Nothing to clear when storage is unavailable.
      }
    },
  };

  const navEntry = performance.getEntriesByType
    ? performance.getEntriesByType("navigation")[0]
    : null;
  if ((navEntry && navEntry.type === "reload") || root.dataset.clear === "1") {
    store.remove(storageKey);
    store.remove(matchedKey);
  }

  const selected = new Set();
  function loadSelection() {
    selected.clear();
    const stored = store.read(storageKey, []);
    (Array.isArray(stored) ? stored : []).filter(isId).forEach((id) => selected.add(id));
  }
  loadSelection();

  // Contacts archived one by one since the last visit no longer exist.
  const forget = (root.dataset.forgetIds || "").split(",").map(Number).filter(isId);
  if (forget.length) {
    forget.forEach((id) => selected.delete(id));
    store.write(storageKey, Array.from(selected));
  }

  // JavaScript owns the payload from here on: rows stop posting on their own.
  rows.forEach((cb) => cb.removeAttribute("name"));

  function persist() {
    store.write(storageKey, Array.from(selected));
  }

  function matchedHere() {
    const matched = store.read(matchedKey, null);
    return matched && matched.query === filterQuery ? matched.count : null;
  }

  function forgetMatch() {
    store.remove(matchedKey);
  }

  function setError(message) {
    if (!errorOut) return;
    errorOut.textContent = message || "";
    errorOut.hidden = !message;
  }

  function describe(n, onPage, pageFull) {
    const matched = matchedHere();
    if (n === 0) return { text: "Nenhum contato selecionado", warn: false };
    if (matched !== null && pageFull) {
      const extra = n - matched;
      const base = `Todos os ${contacts(matched)} ${scope} estão selecionados`;
      return extra > 0
        ? { text: `${base}, mais ${fmt(extra)} de outras buscas`, warn: true }
        : { text: base, warn: false };
    }
    const elsewhere = n - onPage;
    if (elsewhere > 0) {
      const here = onPage === 0 ? "nenhum nesta página" : `${fmt(onPage)} nesta página`;
      return {
        text: `${contacts(n)} ${n === 1 ? "selecionado" : "selecionados"}: ${here}, ${fmt(elsewhere)} em outras páginas ou buscas`,
        warn: true,
      };
    }
    return { text: `${contacts(n)} ${n === 1 ? "selecionado" : "selecionados"}`, warn: false };
  }

  function render() {
    const n = selected.size;
    const onPage = rows.filter((cb) => selected.has(Number(cb.value))).length;
    const pageFull = rows.length > 0 && onPage === rows.length;

    rows.forEach((cb) => {
      cb.checked = selected.has(Number(cb.value));
      cb.closest("tr")?.classList.toggle("is-selected", cb.checked);
    });

    if (pageToggle) {
      pageToggle.checked = pageFull;
      pageToggle.indeterminate = onPage > 0 && !pageFull;
    }

    const state = describe(n, onPage, pageFull);
    if (countOut) {
      countOut.textContent = state.text;
      countOut.classList.toggle("is-warning", state.warn);
    }

    if (matchBtn) {
      const offer = pageFull && totalMatching > rows.length && matchedHere() === null;
      matchBtn.hidden = !offer;
      if (offer && matchBtn.getAttribute("aria-busy") !== "true") {
        matchBtn.textContent = `Selecionar todos os ${contacts(totalMatching)} ${scope}`;
      }
    }

    if (submit) {
      submit.disabled = n === 0;
      submit.textContent = n === 0 ? "Arquivar selecionados" : `Arquivar selecionados (${fmt(n)})`;
    }
    if (hiddenIds) hiddenIds.value = Array.from(selected).join(",");
    if (clearBtn) clearBtn.hidden = n === 0;
    if (bar) {
      // With no rows on screen the bar only matters while something is selected.
      bar.hidden = rows.length === 0 && n === 0;
      bar.classList.toggle("is-active", n > 0);
    }
  }

  function failMatch(message) {
    setError(message);
    matchBtn.removeAttribute("aria-busy");
    render();
    matchBtn.focus();
  }

  async function selectMatching() {
    if (!idsUrl || !matchBtn || matchBtn.getAttribute("aria-busy") === "true") return;
    setError("");
    // aria-busy instead of disabled: a disabled button drops keyboard focus to <body>.
    matchBtn.setAttribute("aria-busy", "true");
    matchBtn.textContent = "Selecionando…";

    let resp;
    try {
      resp = await fetch(filterQuery ? `${idsUrl}?${filterQuery}` : idsUrl, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
    } catch (_err) {
      failMatch("Sem conexão com o servidor. Confira a rede e tente de novo.");
      return;
    }

    // An expired session follows the redirect and answers 200 with the login page.
    const isJson = (resp.headers.get("Content-Type") || "").includes("application/json");
    if (!isJson) {
      failMatch(
        resp.redirected
          ? "Sua sessão expirou. Entre de novo para selecionar todos."
          : "Não foi possível selecionar todos. Tente de novo."
      );
      return;
    }
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !Array.isArray(data.ids)) {
      failMatch((data && data.error) || "Não foi possível selecionar todos. Tente de novo.");
      return;
    }

    const ids = data.ids.filter(isId);
    ids.forEach((id) => selected.add(id));
    store.write(matchedKey, { query: filterQuery, count: ids.length });
    persist();
    matchBtn.removeAttribute("aria-busy");
    render();
    // The button is gone now; keep keyboard users on the result instead of <body>.
    countOut?.focus();
  }

  rows.forEach((cb) => {
    cb.addEventListener("change", () => {
      const id = Number(cb.value);
      if (cb.checked) selected.add(id);
      else {
        selected.delete(id);
        forgetMatch();
      }
      persist();
      render();
    });
  });

  if (pageToggle) {
    pageToggle.addEventListener("change", () => {
      const select = pageToggle.checked;
      rows.forEach((cb) => {
        const id = Number(cb.value);
        if (select) selected.add(id);
        else selected.delete(id);
      });
      if (!select) forgetMatch();
      persist();
      render();
    });
  }

  if (matchBtn) matchBtn.addEventListener("click", selectMatching);

  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      selected.clear();
      store.remove(storageKey);
      forgetMatch();
      setError("");
      render();
      // With no rows the bar hides itself, so fall back to the search box.
      (pageToggle || document.getElementById("contact-search"))?.focus();
    });
  }

  if (bar) {
    bar.addEventListener("submit", (event) => {
      if (selected.size === 0) {
        event.preventDefault();
        return;
      }
      if (hiddenIds) hiddenIds.value = Array.from(selected).join(",");
    });
  }

  // Restore the checked state when the page comes back from the bfcache.
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      loadSelection();
      render();
    }
  });

  render();
})();
