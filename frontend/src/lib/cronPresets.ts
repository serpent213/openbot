/* The Scheduled page's repeat presets as five-field cron rules, and plain words for them. */

export const weekdays = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
export type Preset = "daily" | "weekdays" | "weekly" | "hourly" | "custom";
export const presets: { id: Preset; label: string }[] = [
  { id: "weekdays", label: "Weekdays" }, { id: "daily", label: "Every day" }, { id: "weekly", label: "Once a week" },
  { id: "hourly", label: "Every hour" }, { id: "custom", label: "Cron expression" },
];

/** The five-field rule for a preset; `time` is "HH:MM". */
export function presetCron(preset: Preset, time: string, weekday: number, custom: string): string {
  const [h, m] = time.split(":").map(n => String(Number(n)));
  switch (preset) {
    case "daily": return `${m} ${h} * * *`;
    case "weekdays": return `${m} ${h} * * 1-5`;
    case "weekly": return `${m} ${h} * * ${weekday}`;
    case "hourly": return `${m} * * * *`;
    default: return custom.trim();
  }
}

/** Plain words for rules the presets produce; anything else shows as written. */
export function describeCron(cron: string): string {
  const pad = (n: string) => n.padStart(2, "0");
  let match = /^(\d+) (\d+) \* \* (\*|1-5|[0-7])$/.exec(cron);
  if (match) {
    const at = `${pad(match[2])}:${pad(match[1])}`;
    if (match[3] === "*") return `Every day at ${at}`;
    if (match[3] === "1-5") return `Weekdays at ${at}`;
    return `${weekdays[Number(match[3]) % 7]}s at ${at}`;
  }
  match = /^(\d+) \* \* \* \*$/.exec(cron);
  if (match) return `Every hour at :${pad(match[1])}`;
  return cron;
}
