(function () {
  "use strict";

  window.TrelloPowerUp.initialize({
    "card-buttons": function () {
      return [
        {
          text: "Mark Read",
          condition: "edit",
          callback: function (t) {
            return t.popup({
              title: "Mark Read",
              url: "./mark-read.html",
              height: 200
            });
          }
        }
      ];
    }
  });
})();