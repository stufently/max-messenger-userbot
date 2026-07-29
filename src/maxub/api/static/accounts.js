"use strict";

// Список аккаунтов и добавление нового.
//
// Состояние обновляется опросом /web/api/state, а не потоком событий: браузер
// не умеет слать заголовок Authorization при открытии WebSocket, а класть токен
// в строку запроса нельзя — она оседает в логах.

const STATE_NAMES = {
  new: "новый",
  auth_required: "нужен вход",
  connecting: "подключается",
  syncing: "синхронизация",
  ready: "готов",
  backoff: "пауза после ошибки",
  disabled: "отключён",
};

const REFRESH_MS = 3000;

async function refresh() {
  let data;
  try {
    data = await api("GET", "/web/api/state");
  } catch (error) {
    return; // молча: опрос идёт постоянно, и повторять жалобу каждые три секунды незачем
  }
  const status = data.status;
  $("summary").textContent =
    `транспорт ${status.transport} · аккаунтов ${status.accounts_total} · готовы ${status.accounts_ready}`;
  render(data.accounts);
}

function render(accounts) {
  const body = $("accounts");
  body.textContent = "";
  $("empty").hidden = accounts.length > 0;
  for (const account of accounts) {
    const row = document.createElement("tr");
    row.append(
      cell(String(account.id)),
      cell(account.phone),
      cell(account.label || "—"),
      stateCell(account.state),
      cell(account.last_error || "", "error"),
      actionsCell(account),
    );
    body.append(row);
  }
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text; // только textContent: телефон и текст ошибки приходят извне
  if (className) td.className = className;
  return td;
}

function stateCell(state) {
  const td = cell(STATE_NAMES[state] || state);
  td.className = "state state-" + state;
  return td;
}

function actionsCell(account) {
  const td = document.createElement("td");
  td.className = "actions";
  td.append(
    button("Выслать код", () => startPhone(account)),
    button("QR-код", () => startQr(account)),
    button("Отключить", () => disable(account), account.state === "disabled"),
  );
  return td;
}

function button(text, handler, disabled) {
  const element = document.createElement("button");
  element.type = "button";
  element.className = "ghost";
  element.textContent = text;
  element.disabled = Boolean(disabled);
  element.addEventListener("click", handler);
  return element;
}

$("add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const phone = $("phone").value.trim();
  const label = $("label").value.trim();
  try {
    await api("POST", "/web/api/accounts", { phone, label: label || null });
    $("phone").value = "";
    $("label").value = "";
    say(`Аккаунт ${phone} добавлен. Теперь авторизуйте его.`, "ok");
    refresh();
  } catch (error) {
    say(error.message, "error");
  }
});
