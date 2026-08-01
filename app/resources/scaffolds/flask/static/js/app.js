// {{PROJECT_NAME}} — progressive enhancement only.
//
// The pages work with JavaScript disabled: every form is a real
// <form method="post" action="..."> that Flask handles server-side. Nothing in
// this file is allowed to be load-bearing — it may only improve what already
// works. That is what keeps buttons from silently doing nothing.
//
// No CDN scripts, no build step, no dependencies.

(function () {
  "use strict";

  // Mark the nav link for the current page, so base.html does not need to know
  // which page it is rendering.
  function markActiveNavLink() {
    var here = window.location.pathname.replace(/\/+$/, "") || "/";
    var links = document.querySelectorAll(".site-nav a");
    for (var i = 0; i < links.length; i++) {
      var href = links[i].getAttribute("href") || "";
      var path = href.replace(/\/+$/, "") || "/";
      if (path === here) {
        links[i].classList.add("active");
      }
    }
  }

  // Stop the double-submit that creates duplicate rows on a slow POST.
  function guardSubmitButtons() {
    var forms = document.querySelectorAll("form");
    for (var i = 0; i < forms.length; i++) {
      forms[i].addEventListener("submit", function (event) {
        var button = event.currentTarget.querySelector(
          'button[type="submit"], button:not([type])'
        );
        if (button) {
          button.disabled = true;
          // Re-enable if the browser restores the page from cache.
          window.setTimeout(function () {
            button.disabled = false;
          }, 5000);
        }
      });
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    markActiveNavLink();
    guardSubmitButtons();
  });
})();
