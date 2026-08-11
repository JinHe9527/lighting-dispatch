function pad(value) {
  return String(value).padStart(2, "0");
}

function dateString(date = new Date()) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function timeString(date = new Date()) {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function shortDate(value) {
  const text = String(value || "");
  return /^\d{4}-\d{2}-\d{2}$/.test(text) ? text.slice(5).replace("-", "/") : text;
}

function shortTime(value) {
  const text = String(value || "");
  const match = text.match(/(\d{2}:\d{2})/);
  return match ? match[1] : "--:--";
}

module.exports = { dateString, timeString, shortDate, shortTime };
