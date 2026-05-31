// Focus Button — ESP32 firmware
// Reads a button on GPIO 26. On press, sends POST /press to the Cloudflare Worker.
// Onboard LED: blinks while connecting to WiFi, solid when connected.

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>

// ===== CONFIG =====
const char* WIFI_SSID = "Demirkans-2Gz";
const char* WIFI_PASSWORD = "38030Kayseri!";
const char* SERVER_URL = "https://focus-button-backend.unhook.workers.dev/press";
const char* AUTH_TOKEN = "966c7278e3d618c677d8772e612ea6f79e6d575d53bac63229d3af670c02b28a";
const int DURATION_MINUTES = 1;

const int BUTTON_PIN = 26;
const int LED_PIN = 2;  // Onboard LED on most ESP32 dev boards
const unsigned long DEBOUNCE_DELAY_MS = 50;
const unsigned long BLINK_INTERVAL_MS = 250;

// ===== STATE =====
int lastStableState = HIGH;
int lastReading = HIGH;
unsigned long lastDebounceTime = 0;

// ===== SETUP =====
void setup() {
  Serial.begin(115200);
  delay(100);  // Give the serial monitor a moment to attach
  Serial.println();
  Serial.println("=== Focus Button starting ===");

  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  connectToWiFi();
}

void connectToWiFi() {
  Serial.print("Connecting to WiFi: ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();
  unsigned long lastBlink = 0;
  bool ledState = false;

  while (WiFi.status() != WL_CONNECTED && millis() - start < 30000) {
    // Non-blocking blink while waiting
    if (millis() - lastBlink >= BLINK_INTERVAL_MS) {
      ledState = !ledState;
      digitalWrite(LED_PIN, ledState ? HIGH : LOW);
      lastBlink = millis();
      Serial.print(".");
    }
    delay(10);  // Small yield so the watchdog stays happy
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    digitalWrite(LED_PIN, HIGH);  // Solid on when connected
    Serial.print("Connected! IP: ");
    Serial.println(WiFi.localIP());
  } else {
    digitalWrite(LED_PIN, LOW);   // Off if connection failed
    Serial.println("Failed to connect to WiFi. Will retry on next press.");
  }
}

// ===== LOOP =====
void loop() {
  int reading = digitalRead(BUTTON_PIN);

  // If the reading changed (raw, possibly noisy), reset debounce timer
  if (reading != lastReading) {
    lastDebounceTime = millis();
  }

  // If reading has been stable for DEBOUNCE_DELAY_MS, accept it as the real state
  if ((millis() - lastDebounceTime) > DEBOUNCE_DELAY_MS) {
    if (reading != lastStableState) {
      lastStableState = reading;

      // Detect falling edge: HIGH → LOW = press
      if (lastStableState == LOW) {
        Serial.println("Button pressed!");

        // Visual feedback: blink LED off, then restore to its WiFi-status state
        digitalWrite(LED_PIN, LOW);
        delay(100);
        digitalWrite(LED_PIN, WiFi.status() == WL_CONNECTED ? HIGH : LOW);

        sendButtonPressedEvent();
      }
    }
  }

  lastReading = reading;
}

// ===== HTTP =====
void sendButtonPressedEvent() {
  // Make sure WiFi is connected before trying
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi not connected, attempting reconnect...");
    connectToWiFi();
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("Reconnect failed, abandoning press.");
      return;
    }
  }

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + AUTH_TOKEN);
  http.addHeader("User-Agent", "focus-button-esp32/1.0");

  String payload = String("{\"duration_minutes\":") + DURATION_MINUTES + "}";
  Serial.print("POSTing: ");
  Serial.println(payload);

  int responseCode = http.POST(payload);
  String responseBody = http.getString();

  Serial.print("HTTP ");
  Serial.print(responseCode);
  Serial.print(" - ");
  Serial.println(responseBody);

  http.end();
}