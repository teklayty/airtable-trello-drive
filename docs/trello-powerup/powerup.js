(function () {
  "use strict";

  window.TrelloPowerUp.initialize({
    "card-buttons": function (t, opts) {
      return [
        {
          text: "Mark Read",
          callback: function (t, opts) {
            return t.popup({
              title: "Mark Read",
              items: [
                {
                  text: "BUTTON CALLBACK IS WORKING"
                }
              ]
            });
          }
        }
      ];
    }
  });
})();