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
              url: "https://teklayty.github.io/airtable-trello-drive/trello-powerup/mark-read-test.html",
              height: 180
            });
          }
        }
      ];
    }
  });
})();