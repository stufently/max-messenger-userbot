"use strict";

// Вход в панель и выход из неё. Загружается последним: здесь же стартовая
// проверка, а она обращается к функциям из остальных файлов.

function lock() {
  UI.csrf = null;
  clearInterval(UI.timer);
  UI.timer = null;
  stopQr();
  $("workspace").hidden = true;
  $("gate").hidden = false;
  $("logout").hidden = true;
  $("summary").textContent = "";
}

function unlock(csrf) {
  UI.csrf = csrf;
  $("gate").hidden = true;
  $("workspace").hidden = false;
  $("logout").hidden = false;
  $("token").value = "";
  refresh();
  clearInterval(UI.timer);
  UI.timer = setInterval(refresh, REFRESH_MS);
}

$("token-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const opened = await api("POST", "/web/session", { token: $("token").value });
    say("");
    unlock(opened.csrf);
  } catch (error) {
    say(error.message, "error");
  }
});

$("logout").addEventListener("click", async () => {
  try {
    await api("DELETE", "/web/session");
  } catch (error) {
    // Сессия могла истечь сама — тогда выходить уже не из чего.
  }
  lock();
  say("Сессия закрыта.", "ok");
});

// Cookie могла пережить перезагрузку страницы — тогда токен вводить не нужно.
api("GET", "/web/session")
  .then((info) => (info.authenticated ? unlock(info.csrf) : lock()))
  .catch(() => lock());
