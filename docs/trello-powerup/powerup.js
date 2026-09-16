(function () {
  "use strict";

  window.TrelloPowerUp.initialize({
    "card-buttons": function () {
      return [
        {
          text: "Mark Read",
          condition: "edit"
        }
      ];
    }
  });
})();