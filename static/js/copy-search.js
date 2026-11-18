// Copy suggestions for the Issue form: type at least 3 characters of a title, copy ID or barcode
// and choose a copy from the list. Plain DOM code (no inline scripts, allowed by the CSP); every
// value from the server is written with textContent, never as HTML.
(function () {
  "use strict";
  var MIN = 3;
  var DELAY_MS = 250;

  document.querySelectorAll("input[data-copy-search]").forEach(function (input) {
    var url = input.getAttribute("data-copy-search");
    var box = document.createElement("div");
    box.id = input.id + "_suggestions";
    box.className = "copy-suggestions";
    box.setAttribute("role", "listbox");
    box.setAttribute("aria-label", "Matching copies");
    box.hidden = true;
    input.insertAdjacentElement("afterend", box);
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-controls", box.id);
    input.setAttribute("aria-expanded", "false");

    var items = [];
    var active = -1;
    var timer = null;
    var request = 0;

    function hide() {
      box.hidden = true;
      active = -1;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }

    function mark() {
      box.querySelectorAll(".copy-option").forEach(function (option, index) {
        option.setAttribute("aria-selected", String(index === active));
        option.classList.toggle("active", index === active);
      });
      if (active >= 0) {
        input.setAttribute("aria-activedescendant", box.id + "_" + active);
      } else {
        input.removeAttribute("aria-activedescendant");
      }
    }

    function choose(index) {
      var copy = items[index];
      if (!copy) return;
      input.value = copy.copy_code;
      hide();
      input.focus();
    }

    function render(results, message) {
      box.textContent = "";
      items = results;
      active = -1;
      if (message) {
        var note = document.createElement("p");
        note.className = "text-muted-token small copy-note";
        note.textContent = message;
        box.appendChild(note);
      }
      results.forEach(function (copy, index) {
        var option = document.createElement("button");
        option.type = "button";
        option.className = "copy-option";
        option.id = box.id + "_" + index;
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        var title = document.createElement("strong");
        title.textContent = copy.title + (copy.author ? " — " + copy.author : "");
        var detail = document.createElement("span");
        detail.className = "text-muted-token small d-block";
        detail.textContent =
          copy.copy_code + " · " + copy.condition + " · " + copy.status +
          (copy.issuable ? "" : " (cannot be issued)");
        option.appendChild(title);
        option.appendChild(detail);
        // mousedown so the choice lands before the input loses focus
        option.addEventListener("mousedown", function (event) {
          event.preventDefault();
          choose(index);
        });
        box.appendChild(option);
      });
      box.hidden = !(results.length || message);
      input.setAttribute("aria-expanded", String(!box.hidden));
    }

    input.addEventListener("input", function () {
      clearTimeout(timer);
      var query = input.value.trim();
      if (query.length < MIN) {
        hide();
        return;
      }
      timer = setTimeout(function () {
        var mine = ++request;
        fetch(url + "?q=" + encodeURIComponent(query), {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        })
          .then(function (response) {
            if (!response.ok) throw new Error("search failed");
            return response.json();
          })
          .then(function (data) {
            if (mine !== request || input.value.trim() !== query) return;
            render(
              data.results,
              data.results.length ? "" : 'No copies match "' + query + '".'
            );
          })
          .catch(function () {
            render([], "Search is unavailable. Type the copy ID instead.");
          });
      }, DELAY_MS);
    });

    input.addEventListener("keydown", function (event) {
      if (box.hidden) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        active = Math.min(items.length - 1, active + 1);
        mark();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        active = Math.max(0, active - 1);
        mark();
      } else if (event.key === "Enter" && active >= 0) {
        event.preventDefault();
        choose(active);
      } else if (event.key === "Escape") {
        hide();
      }
    });

    input.addEventListener("blur", hide);
  });
})();
