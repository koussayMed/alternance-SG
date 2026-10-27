import http from "k6/http";
import { check, fail, sleep } from "k6";

const BASE_URL = (__ENV.BASE_URL || "").replace(/\/+$/, "");
const VUS = parseInt(__ENV.VUS || "100", 10);
const RAMP = __ENV.RAMP || "2m";
const PLATEAU = __ENV.PLATEAU || "4m";

const VISITORS = 50; // simulated visitors, each following 3 cities
const CITIES = ["Paris", "Lyon", "Marseille", "Lille", "Bordeaux", "Nantes", "Tunis", "Sfax"];
const SEARCHES = ["par", "lyo", "mars", "lil", "bord", "nan", "tun", "sfa"];

export const options = {
  scenarios: {
    montee_en_charge: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "30s", target: Math.max(1, Math.ceil(VUS / 5)) }, // warm-up
        { duration: RAMP, target: VUS }, // ramp-up: queues grow, Azure adds replicas
        { duration: PLATEAU, target: VUS }, // plateau: latency should drop once replicas are ready
        { duration: "30s", target: 0 },
      ],
      gracefulRampDown: "10s",
    },
  },
  // Service objectives, measured during the load only (setup excluded); k6 fails if they are missed
  thresholds: {
    "http_req_failed{scenario:montee_en_charge}": ["rate<0.01"], // under 1% errors
    // Same objective per route, so the summary shows which one fails
    "http_req_failed{endpoint:weather}": ["rate<0.01"],
    "http_req_failed{endpoint:geocode}": ["rate<0.01"],
    "http_req_duration{endpoint:weather}": ["p(95)<1500"], // 95% of weather lists under 1.5 s
    "checks{scenario:montee_en_charge}": ["rate>0.99"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

// Fixed visitor ids (UUID format): reruns reuse the same lists
function visitorId(n) {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

function pick(list) {
  return list[Math.floor(Math.random() * list.length)];
}

export function setup() {
  if (!BASE_URL.startsWith("http")) {
    fail("BASE_URL manquante : -e BASE_URL=https://...");
  }
  const health = http.get(`${BASE_URL}/healthz`);
  if (health.status !== 200) {
    fail(`application injoignable (${BASE_URL}/healthz : ${health.status})`);
  }
  // Each visitor follows 3 cities (201 = added, 409 = already there from a previous run)
  let ok = 0;
  for (let n = 1; n <= VISITORS; n++) {
    for (let k = 0; k < 3; k++) {
      const res = http.post(
        `${BASE_URL}/api/cities`,
        JSON.stringify({ name: CITIES[(n + k) % CITIES.length] }),
        {
          headers: { "Content-Type": "application/json", "X-Visitor-Id": visitorId(n) },
          tags: { endpoint: "setup" },
          responseCallback: http.expectedStatuses(201, 409),
        }
      );
      if (res.status === 201 || res.status === 409) ok++;
    }
  }
  if (ok === 0) fail("impossible de préparer les listes de villes");
  console.log(`préparation : ${ok}/${VISITORS * 3} villes en place pour ${VISITORS} visiteurs`);
}

// One visitor journey, looped by every virtual user
export default function () {
  const visitor = visitorId(1 + ((__VU + __ITER) % VISITORS));
  const roll = Math.random();

  if (roll < 0.7) {
    // Main case: the home page loads the visitor's list with its weather (one aggregated SQL query)
    const res = http.get(`${BASE_URL}/api/weather`, {
      headers: { "X-Visitor-Id": visitor },
      tags: { endpoint: "weather" },
    });
    check(res, {
      "météo : 200": (r) => r.status === 200,
      "météo : liste de villes": (r) => r.status === 200 && Array.isArray(r.json()) && r.json().length > 0,
    });
  } else if (roll < 0.85) {
    // Typing in the search box: suggestions
    const res = http.get(`${BASE_URL}/api/geocode?q=${pick(SEARCHES)}`, { tags: { endpoint: "geocode" } });
    check(res, { "suggestions : 200": (r) => r.status === 200 });
  } else if (roll < 0.95) {
    // Static page served by nginx (no backend)
    const res = http.get(`${BASE_URL}/`, { tags: { endpoint: "page" } });
    check(res, { "page : 200": (r) => r.status === 200 });
  } else {
    const res = http.get(`${BASE_URL}/api/info`, { tags: { endpoint: "info" } });
    check(res, { "info : 200": (r) => r.status === 200 });
  }

  sleep(Math.random() * 0.2); // very short pause: many requests in flight, to trigger scaling
}
