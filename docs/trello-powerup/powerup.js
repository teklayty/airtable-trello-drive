(function () {
  "use strict";

  function showMarkRead(t) {
    return t.popup({
      title: "Mark Read",
      url: "./mark-read.html",
      height: 220
    });
  }

  window.TrelloPowerUp.initialize({
    "card-buttons": function () {
      return [
        {
          text: "Mark Read",
          callback: showMarkRead
        }
      ];
    }
  });
})();