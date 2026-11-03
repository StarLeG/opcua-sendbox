/* Переводы строк JS-модулей (RU → EN). Вызывается как t("русская строка", {vars}). */
(function () {
  "use strict";
  var EN = {
    " или ": " or ",
    "OPC UA клиентов: {n}": "OPC UA clients: {n}",
    "Активировать": "Activate",
    "Аларм {name} добавлен": "Alarm {name} added",
    "Ваши сессии:": "Your sessions:",
    "Закрыть сессию? Сервер остановится, клиенты отключатся.": "Close the session? The server stops and clients disconnect.",
    "Запускаем сервер…": "Starting the server…",
    "Конфигурация сохранена: {name}": "Configuration saved: {name}",
    "Файл не является JSON": "The file is not valid JSON",
    "В файле нет списков tags или alarms": "The file has no tags or alarms lists",
    "Клиент записал {tag} = {value}": "Client wrote {tag} = {value}",
    "Личного сервера пока нет. Создайте его — это займёт пару секунд.": "No personal server yet. Create one — it takes a couple of seconds.",
    "Ошибка {status}": "Error {status}",
    "Сбросить": "Clear",
    "Сервер готов: подключите клиент к {url}": "Server ready: connect your client to {url}",
    "Сервер сессии упал. Создайте новую сессию.": "The session server crashed. Create a new session.",
    "Сессия закрыта ({reason}).": "Session closed ({reason}).",
    "Сессия закрыта. Можно создать новую.": "The session is closed. You can create a new one.",
    "Скопировано": "Copied",
    "Скопировано: {id}.": "Copied: {id}.",
    "Скопируйте адрес": "Copy the address",
    "Тег {name} добавлен: ns=2;s=Tags.{name}": "Tag {name} added: ns=2;s=Tags.{name}",
    "Удалить": "Delete",
    "активен": "active",
    "вручную": "manual",
    "запись": "write",
    "каждые {s} с": "every {s} s",
    "мин": "min",
    "не идёт: подключены клиенты": "paused: clients connected",
    "недоступен": "unavailable",
    "норма": "normal",
    "перезапускается": "restarting",
    "работает": "running",
    "с": "s",
    "ч": "h"
  };
  window.t = function (s, vars) {
    var out = (window.LANG === "en" && EN[s] !== undefined) ? EN[s] : s;
    if (vars) {
      for (var key in vars) {
        out = out.split("{" + key + "}").join(String(vars[key]));
      }
    }
    return out;
  };
})();
