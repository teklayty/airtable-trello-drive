(function () {
  "use strict";

  window.TrelloPowerUp.initialize({
    "card-buttons": function () {
      return [
        {
          text: "Mark Read",
          callback: function (t) {
            return t.popup({
              title: "Mark Read",
              url: "./mark-read.html",
              height: 220
            });
          }
        }
      ];
    }
  });
})();