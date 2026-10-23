// Aiuti dell'interfaccia della cabina.
//
// Servono solo a rendere comode le pagine: nessun controllo di sicurezza
// dipende da questo codice. La validità della scheda dipende dalle prove:
// con "dispositivo scorretto" i controlli della scheda si spengono e la
// bacheca rifiuta comunque una scheda irregolare.

(function () {
  "use strict";

  function all(root, selector) {
    return Array.prototype.slice.call(root.querySelectorAll(selector));
  }

  // Durata a parole, come format_duration in preparazione.py.
  function duration(seconds) {
    if (seconds < 1) {
      return "meno di un secondo";
    }
    if (seconds < 1.5) {
      return "circa un secondo";
    }
    if (seconds < 60) {
      return "circa " + Math.round(seconds) + " secondi";
    }
    if (seconds < 90) {
      return "circa un minuto";
    }
    return "circa " + Math.round(seconds / 60) + " minuti";
  }

  // Moduli lenti: al primo invio i pulsanti si disattivano (niente doppi
  // clic) e compare un messaggio con i secondi trascorsi.
  all(document, "form[data-busy]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var message = form.querySelector(".busy");
      var start = Date.now();

      window.setTimeout(function () {
        all(document, "form[data-busy] button").forEach(function (button) {
          button.disabled = true;
        });
      }, 0);

      if (!message) {
        return;
      }

      message.hidden = false;
      message.textContent = form.dataset.busy;

      window.setInterval(function () {
        var seconds = Math.round((Date.now() - start) / 1000);
        message.textContent = form.dataset.busy + " " + seconds + " s";
      }, 1000);
    });
  });

  // Tornando indietro con il browser la pagina può riapparire com'era
  // dopo l'invio: si riattivano i pulsanti.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) {
      return;
    }

    all(document, "form[data-busy] button").forEach(function (button) {
      button.disabled = false;
    });

    all(document, "form[data-busy] .busy").forEach(function (message) {
      message.hidden = true;
    });
  });

  // Pulsante «Copia il codice» della ricevuta.
  all(document, "[data-copy]").forEach(function (button) {
    var label = button.querySelector("span");

    button.addEventListener("click", function () {
      var source = document.getElementById(button.dataset.copy);

      if (!source || !navigator.clipboard) {
        return;
      }

      navigator.clipboard.writeText(source.textContent.trim()).then(function () {
        if (label) {
          label.textContent = "Copiato";
        }
      });
    });
  });

  // Pagina «Prepara»: limite degli elettori e tempo stimato.
  var prepare = document.getElementById("prepara");

  if (prepare) {
    var districts = Number(prepare.dataset.districts);
    var voters = document.getElementById("elettori");
    var estimate = document.getElementById("stima");

    var update = function () {
      var mode = prepare.querySelector('input[name="modalita"]:checked');
      var spoil = prepare.querySelector('input[name="sfide"]');

      if (!mode) {
        return;
      }

      voters.max = mode.dataset.max;

      var count = Number(voters.value);
      var max = Number(mode.dataset.max);

      if (!(count >= 0 && count <= max)) {
        estimate.textContent = "elettori da 0 a " + max + " per circoscrizione";
        return;
      }

      var ballots = count * districts * (1 + Number(spoil.value || 0) / 100);

      estimate.textContent = duration(ballots * Number(mode.dataset.seconds));
    };

    all(prepare, "input").forEach(function (input) {
      input.addEventListener("input", update);
      input.addEventListener("change", update);
    });

    update();
  }

  // Pagina di avanzamento: chiede lo stato ogni secondo e, quando la
  // simulazione è pronta, passa alla pagina «Prepara».
  var progress = document.getElementById("avanzamento");

  if (progress) {
    var bar = document.getElementById("barra");
    var counter = document.getElementById("conteggio");
    var remaining = document.getElementById("rimanente");
    var ceremony = document.getElementById("fase-cerimonia");
    var voting = document.getElementById("fase-voto");

    var poll = function () {
      fetch(progress.dataset.statusUrl, { cache: "no-store" })
        .then(function (response) {
          return response.json();
        })
        .then(function (status) {
          if (!status.running) {
            window.location.href = status.phase === "errore"
              ? progress.dataset.formUrl
              : progress.dataset.readyUrl;
            return;
          }

          bar.max = status.total || 1;
          bar.value = status.done;
          counter.textContent = status.done + " elettori su " + status.total;
          remaining.textContent = status.remaining === null
            ? ""
            : "· mancano " + duration(status.remaining);

          if (status.phase === "voto") {
            ceremony.className = "done";
            voting.className = "current";
          }

          window.setTimeout(poll, 1000);
        })
        .catch(function () {
          window.setTimeout(poll, 2000);
        });
    };

    window.setTimeout(poll, 500);
  }

  // Scheda: una preferenza segna la sua lista, le preferenze di altre
  // liste vengono tolte, il limite R4 viene segnalato. Se le preferenze
  // sono tutte dello stesso genere la pagina avvisa che, come prevede
  // la legge, varrà solo la prima in ordine di lista: l'annullamento lo
  // fa il dispositivo prima di cifrare.
  var form = document.getElementById("scheda");

  if (!form) {
    return;
  }

  var maxPreferences = Number(form.dataset.max);
  var notice = document.getElementById("avviso");
  var faulty = document.getElementById("scorretto");

  function preferences() {
    return all(form, 'input[name="preferenze"]');
  }

  function listRadios() {
    return all(form, 'input[name="lista"]');
  }

  function warn(message) {
    notice.textContent = message;
    notice.hidden = !message;
  }

  function highlight() {
    listRadios().forEach(function (radio) {
      var card = radio.closest(".list-card");

      if (card) {
        card.classList.toggle("selected", radio.checked);
      }
    });
  }

  function selectList(value) {
    listRadios().forEach(function (radio) {
      radio.checked = radio.value === value;
    });
  }

  function clearOtherLists(value) {
    preferences().forEach(function (box) {
      if (box.dataset.list !== value) {
        box.checked = false;
      }
    });
  }

  function chosenPreferences() {
    return preferences().filter(function (box) {
      return box.checked;
    });
  }

  // Restituisce true se ha dovuto togliere una preferenza di troppo.
  function checkLimit(changed) {
    if (chosenPreferences().length > maxPreferences) {
      changed.checked = false;
      warn("Al massimo " + maxPreferences + " preferenze (regola R4).");
      return true;
    }

    return false;
  }

  function genderNotice() {
    var chosen = chosenPreferences();

    var sameGender = chosen.length > 1 && chosen.every(function (box) {
      return box.dataset.gender === chosen[0].dataset.gender;
    });

    warn(
      sameGender
        ? "Preferenze tutte dello stesso genere: per legge vale solo la " +
          "prima in ordine di lista, le altre saranno annullate."
        : ""
    );
  }

  preferences().forEach(function (box) {
    box.addEventListener("change", function () {
      if (faulty.checked) {
        highlight();
        return;
      }

      if (box.checked) {
        selectList(box.dataset.list);
        clearOtherLists(box.dataset.list);

        if (checkLimit(box)) {
          highlight();
          return;
        }
      }

      genderNotice();
      highlight();
    });
  });

  listRadios().forEach(function (radio) {
    radio.addEventListener("change", function () {
      if (!faulty.checked) {
        clearOtherLists(radio.value);
        warn("");
      }

      highlight();
    });
  });

  faulty.addEventListener("change", function () {
    warn(
      faulty.checked
        ? "Controlli dell'interfaccia disattivati: ora decidono solo le prove."
        : ""
    );
  });

  // Dopo un errore la scheda torna con le scelte già fatte.
  highlight();
})();
