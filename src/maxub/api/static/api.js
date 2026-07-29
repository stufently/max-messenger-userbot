"use strict";

// Общение с демоном и показ сообщений — единственное место, где страница знает
// про сеть. Ванильный JS без сборщиков: страницу отдаёт сам демон, и второй
// тулчейн ради нескольких форм не нужен.
//
// Токен демона здесь нигде не хранится. Он уходит один раз в POST /web/session,
// а взамен приходит метка CSRF, которая живёт только в этом объекте: ни
// localStorage, ни cookie, доступной скрипту. После перезагрузки страницы метка
// берётся заново из GET /web/session по HttpOnly-cookie.

const UI = { csrf: null, timer: null };

const $ = (id) => document.getElementById(id);

async function api(method, path, body) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  // Изменяющие запросы демон принимает только с этой меткой — иначе чужая
  // страница в том же браузере смогла бы дёргать локальный API.
  if (UI.csrf) headers["X-CSRF-Token"] = UI.csrf;
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!response.ok) {
    if (response.status === 401) lock();
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : `ошибка ${response.status}`);
  }
  return data;
}

function say(text, kind) {
  const box = $("message");
  box.textContent = text;
  box.className = "message" + (kind ? " " + kind : "");
  box.hidden = !text;
}

function describe(account) {
  return account.label ? `${account.phone} (${account.label})` : account.phone;
}
