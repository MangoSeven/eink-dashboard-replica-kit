/*
 * Waveshare ESP32-S3-ePaper-3.97 — 三页家庭看板
 *
 * 页面：主页、股票、每日读书推荐。电脑端渲染 800×480 位图，板子负责配网、
 * 自动发现、下载显示，以及主页右上状态栏的温湿度/Wi-Fi 局部刷新。
 */

#include <SPI.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <time.h>
#include <DNSServer.h>
#include <WebServer.h>
#include <Preferences.h>
#include <GxEPD2_BW.h>
#include <gdem/GxEPD2_397_GDEM0397T81.h>
#include "stock_screen_bitmap.h"
#include "dashboard_config.h"
#include "clock_digits.h"
#include "home_fallback_bitmap.h"
#include "reading_fallback_bitmap.h"
#include "weekday_glyphs.h"

constexpr int EPD_BUSY = 3;
constexpr int EPD_DC = 9;
constexpr int EPD_CS = 10;
constexpr int EPD_SCLK = 11;
constexpr int EPD_MOSI = 12;
constexpr int EPD_RST = 46;

constexpr int BTN_UP = 4;
constexpr int BTN_FUNCTION = 5;
constexpr int BTN_DOWN = 6;
constexpr int SHT_SDA = 41;
constexpr int SHT_SCL = 42;
constexpr uint8_t SHT_ADDR = 0x70;
constexpr uint8_t RTC_ADDR = 0x51;  // 板载 PCF85063，与 SHTC3 共用 I²C GPIO41/42
constexpr uint8_t PMIC_ADDR = 0x34; // AXP2101 电源管理芯片：只读燃料计/电压

constexpr uint32_t FRAME_BYTES = 800UL * 480UL / 8UL;
constexpr uint32_t LOCAL_REFRESH_MS = 60UL * 1000UL;
constexpr uint32_t HOME_REFRESH_MS = DASHBOARD_HOME_REFRESH_MINUTES * 60UL * 1000UL;
constexpr uint32_t STOCK_REFRESH_MS = DASHBOARD_STOCK_REFRESH_MINUTES * 60UL * 1000UL;
constexpr uint32_t READING_REFRESH_MS = DASHBOARD_READING_REFRESH_HOURS * 60UL * 60UL * 1000UL;
// Full clean cadence: e-paper partial waveforms build ghosting over repeated updates.
constexpr uint8_t MAX_HOME_PARTIAL_REFRESHES = 8;
constexpr uint8_t MAX_UNCHANGED_PAGE_CHECKS = 6;

GxEPD2_BW<GxEPD2_397_GDEM0397T81,
           GxEPD2_397_GDEM0397T81::HEIGHT> display(
    GxEPD2_397_GDEM0397T81(EPD_CS, EPD_DC, EPD_RST, EPD_BUSY));

static_assert(sizeof(stock_screen_bitmap) == FRAME_BYTES,
              "股票页位图必须是800×480的48,000字节1-bit数据");
static_assert(READING_FALLBACK_BITMAP_LEN == FRAME_BYTES,
              "读书页位图必须是800×480的48,000字节1-bit数据");

enum Page : uint8_t {
  PAGE_HOME = 0,
  PAGE_STOCK,
  PAGE_READING,
  PAGE_COUNT
};

enum FetchResult : uint8_t { FETCHED, UNCHANGED, FETCH_FAILED };

uint8_t framebuf[FRAME_BYTES];
// 三页最近一次成功的位图放在 PSRAM：切页时先显示缓存，网络短暂波动不会跳到错误页。
uint8_t* pageCache[PAGE_COUNT] = {nullptr, nullptr, nullptr};
bool pageCached[PAGE_COUNT] = {false, false, false};
Page currentPage = PAGE_HOME;
String etags[3];
String wifiSsid = DASHBOARD_WIFI_SSID;
String wifiPassword = DASHBOARD_WIFI_PASSWORD;
String dashboardHost = DASHBOARD_HOST;
bool ntpReady = false;
bool ntpRequested = false;
// Only use RTC after an NTP time has been written to it at least once.
bool rtcTrusted = false;
uint32_t lastWifiAttempt = 0;
uint32_t lastLocalRefresh = 0;
uint32_t lastRemoteRefresh = 0;
Page lastRemotePage = PAGE_COUNT;
uint8_t homePartialRefreshes = 0;
// Invalidate after every full redraw: cached values must never leave base-image blanks.
bool localHomeValid = false;
String lastClockText, lastDateText, lastTemperatureText, lastHumidityText, lastStatusText;
uint8_t unchangedPageChecks[PAGE_COUNT] = {0, 0, 0};
DNSServer dnsServer;
WebServer configServer(80);
bool restartAfterConfigSave = false;
String nearbyNetworkOptions;
WiFiUDP discoveryUdp;
String discoveredHost;

void refreshCurrentPage(bool pageChanged, bool forceFullClean = false);

bool wifiConfigured() {
  // 支持无密码的开放网络。
  return wifiSsid.length() > 0;
}

bool hostConfigured() { return dashboardHost.length() > 0 || discoveredHost.length() > 0; }

bool discoverAgent() {
  if (WiFi.status() != WL_CONNECTED) return false;
  discoveryUdp.begin(18999);
  discoveryUdp.beginPacket(IPAddress(255, 255, 255, 255), 18999);
  discoveryUdp.print("EPAPER_DISCOVER");
  discoveryUdp.endPacket();
  const uint32_t started = millis();
  while (millis() - started < 1500) {
    const int size = discoveryUdp.parsePacket();
    if (size) {
      char reply[80] = {0};
      discoveryUdp.read(reply, min(size, 79));
      String text(reply);
      if (text.startsWith("EPAPER_AGENT 1 ")) {
        discoveredHost = "http://" + discoveryUdp.remoteIP().toString() + ":" + text.substring(15);
        discoveryUdp.stop();
        return true;
      }
    }
    delay(10);
  }
  discoveryUdp.stop();
  return false;
}

void loadStoredSettings() {
  Preferences preferences;
  preferences.begin("dashboard", true);
  const String storedSsid = preferences.getString("wifi_ssid", "");
  const String storedPassword = preferences.getString("wifi_pass", "");
  const String storedHost = preferences.getString("host", "");
  rtcTrusted = preferences.getBool("rtc_trusted", false);
  preferences.end();
  if (storedSsid.length()) wifiSsid = storedSsid;
  if (storedPassword.length()) wifiPassword = storedPassword;
  if (storedHost.length()) dashboardHost = storedHost;
}

String htmlEscape(String value) {
  value.replace("&", "&amp;");
  value.replace("<", "&lt;");
  value.replace(">", "&gt;");
  value.replace("\"", "&quot;");
  return value;
}

void scanNearbyNetworks() {
  nearbyNetworkOptions = "<option value=''>请选择附近的 Wi-Fi</option>";
  const int count = WiFi.scanNetworks(false, true);
  bool savedFound = false;
  for (int i = 0; i < count; ++i) {
    const String ssid = WiFi.SSID(i);
    if (!ssid.length()) continue;
    const bool selected = ssid == wifiSsid;
    savedFound |= selected;
    nearbyNetworkOptions += "<option value='" + htmlEscape(ssid) + "'";
    if (selected) nearbyNetworkOptions += " selected";
    nearbyNetworkOptions += ">" + htmlEscape(ssid) + "</option>";
  }
  if (wifiSsid.length() && !savedFound) {
    nearbyNetworkOptions += "<option value='" + htmlEscape(wifiSsid) + "' selected>" + htmlEscape(wifiSsid) + "（已保存）</option>";
  }
  WiFi.scanDelete();
}

String setupPageHtml(const String& message = "") {
  String html = F("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
                  "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                  "<title>墨水屏配网</title><style>body{max-width:520px;margin:32px auto;"
                  "padding:0 18px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;}"
                  "label{display:block;margin-top:18px;font-weight:600}input{box-sizing:border-box;"
                  "width:100%;padding:12px;margin-top:7px;border:1px solid #aaa;border-radius:8px;font-size:16px}"
                  "select,button{box-sizing:border-box;width:100%;padding:12px;margin-top:7px;border:1px solid #aaa;border-radius:8px;font-size:16px;background:#fff}"
                  "button{margin-top:24px;background:#111;color:#fff}"
                  ".note{color:#555;line-height:1.6}.ok{padding:10px;background:#eef7ee;border-radius:8px}</style>"
                  "<body><h1>墨水屏配网</h1><p class='note'>选择 2.4GHz Wi-Fi 并输入密码。保存后板子会自动重启；电脑看板会在局域网中自动发现。</p>");
  if (message.length()) html += "<p class='ok'>" + htmlEscape(message) + "</p>";
  html += "<form method='post' action='/save'><label>附近的 Wi-Fi</label><select name='ssid' required>" +
          nearbyNetworkOptions + "</select><label>Wi-Fi 密码</label><input name='pass' type='password' "
          "placeholder='留空则保持原密码'><label>电脑看板地址（可选兜底）</label><input name='host' "
          "placeholder='自动发现失败时才填写，例如 http://192.168.1.25:1234' value='" + htmlEscape(dashboardHost) +
          "'><button type='submit'>保存并重启</button></form></body></html>";
  return html;
}

void drawSetupScreen(const String& apName) {
  display.setFullWindow();
  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
    display.setTextColor(GxEPD_BLACK);
    display.setTextSize(3);
    display.setCursor(40, 115);
    display.print("Wi-Fi setup");
    display.setTextSize(2);
    display.setCursor(40, 180);
    display.print("Connect: ");
    display.print(apName);
    display.setCursor(40, 225);
    display.print("Open: 192.168.4.1");
    display.setCursor(40, 285);
    display.print("Select Wi-Fi and save in browser");
    display.setCursor(40, 330);
    display.print("Hold CENTER 3s to exit");
  } while (display.nextPage());
}

void startConfigPortal() {
  WiFi.disconnect(true, false);
  WiFi.mode(WIFI_AP_STA);
  String suffix = WiFi.macAddress();
  suffix.replace(":", "");
  suffix = suffix.substring(max(0, (int)suffix.length() - 4));
  const String apName = "EInk-Setup-" + suffix;
  WiFi.softAP(apName.c_str());
  scanNearbyNetworks();
  drawSetupScreen(apName);

  configServer.on("/", HTTP_GET, []() {
    configServer.send(200, "text/html; charset=utf-8", setupPageHtml());
  });
  configServer.on("/save", HTTP_POST, []() {
    const String ssid = configServer.arg("ssid");
    const String password = configServer.arg("pass");
    const String host = configServer.arg("host");
    if (!ssid.length()) {
      configServer.send(400, "text/html; charset=utf-8", setupPageHtml("请输入 Wi-Fi 名称。"));
      return;
    }
    Preferences preferences;
    preferences.begin("dashboard", false);
    preferences.putString("wifi_ssid", ssid);
    if (password.length()) preferences.putString("wifi_pass", password);
    preferences.putString("host", host);
    preferences.end();
    configServer.send(200, "text/html; charset=utf-8",
                      "<meta charset='utf-8'><h2>已保存，板子正在重启。</h2>");
    restartAfterConfigSave = true;
  });
  configServer.onNotFound([]() {
    configServer.sendHeader("Location", "http://192.168.4.1/", true);
    configServer.send(302, "text/plain", "");
  });
  configServer.begin();
  dnsServer.start(53, "*", WiFi.softAPIP());

  const uint32_t started = millis();
  uint32_t centerPressedAt = 0;
  while (millis() - started < 10UL * 60UL * 1000UL) {
    dnsServer.processNextRequest();
    configServer.handleClient();
    if (restartAfterConfigSave) {
      delay(700);
      ESP.restart();
    }
    if (digitalRead(BTN_FUNCTION) == LOW) {
      if (!centerPressedAt) centerPressedAt = millis();
      if (millis() - centerPressedAt >= 3000) break;
    } else centerPressedAt = 0;
    delay(2);
  }
  dnsServer.stop();
  configServer.stop();
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  refreshCurrentPage(true);
}

uint8_t shtCrc(const uint8_t* bytes, size_t length) {
  uint8_t crc = 0xFF;
  for (size_t i = 0; i < length; ++i) {
    crc ^= bytes[i];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x80) ? (crc << 1) ^ 0x31 : (crc << 1);
    }
  }
  return crc;
}

bool readShtc3(float& temperatureC, float& humidityPct) {
  Wire.beginTransmission(SHT_ADDR);
  Wire.write(0x80);
  Wire.write(0x5D);  // soft reset
  if (Wire.endTransmission() != 0) return false;
  delay(2);

  Wire.beginTransmission(SHT_ADDR);
  Wire.write(0x78);
  Wire.write(0x66);  // normal-power measurement
  if (Wire.endTransmission() != 0) return false;
  delay(15);

  uint8_t bytes[6];
  if (Wire.requestFrom((int)SHT_ADDR, 6) != 6) return false;
  for (uint8_t i = 0; i < 6; ++i) bytes[i] = Wire.read();
  if (shtCrc(bytes, 2) != bytes[2] || shtCrc(bytes + 3, 2) != bytes[5]) {
    return false;
  }

  const uint16_t rawT = (uint16_t(bytes[0]) << 8) | bytes[1];
  const uint16_t rawH = (uint16_t(bytes[3]) << 8) | bytes[4];
  temperatureC = -45.0f + 175.0f * rawT / 65535.0f;
  humidityPct = 100.0f * rawH / 65535.0f;
  return true;
}

void drawFrame(const uint8_t* bitmap, bool fullRefresh) {
  localHomeValid = false;
  if (fullRefresh) {
    display.setFullWindow();
  } else {
    display.setPartialWindow(0, 0, 800, 480);
  }
  display.firstPage();
  do {
    display.fillScreen(GxEPD_BLACK);
    display.drawBitmap(0, 0, bitmap, 800, 480, GxEPD_WHITE);
  } while (display.nextPage());
}

void drawRawFrame(bool fullRefresh) {
  drawFrame(framebuf, fullRefresh);
}

void drawOfflinePage() {
  display.setFullWindow();
  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
    display.setTextColor(GxEPD_BLACK);
    display.setTextSize(3);
    display.setCursor(64, 140);
    display.print("Waiting for page data");
    display.setTextSize(2);
    display.setCursor(64, 205);
    if (!wifiConfigured()) display.print("Hold CENTER 3s for Wi-Fi setup");
    else if (WiFi.status() != WL_CONNECTED) display.print("Wi-Fi not connected: check router/password");
    else if (!dashboardHost.length() && !discoveredHost.length()) display.print("Dashboard service not found");
    else display.print("Download failed; press CENTER to retry");
    display.setCursor(64, 255);
    display.print("UP prev  DOWN next  CENTER refresh");
  } while (display.nextPage());
}

const uint8_t WIFI_ICON[54] PROGMEM = {0x00, 0x7F, 0x00, 0x01, 0xFF, 0xC0, 0x07, 0x80, 0xF0, 0x0E, 0x00, 0x38, 0x1C, 0x00, 0x1C, 0x38, 0x3E, 0x0E, 0x30, 0xFF, 0x86, 0x61, 0xC1, 0xC3, 0x63, 0x00, 0x63, 0x06, 0x00, 0x30, 0x06, 0x1C, 0x30, 0x00, 0x7F, 0x00, 0x00, 0x63, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};

uint8_t bcdToDec(uint8_t value) { return (value >> 4) * 10 + (value & 0x0F); }
uint8_t decToBcd(uint8_t value) { return ((value / 10) << 4) | (value % 10); }

bool readRtcTime(struct tm& timeInfo) {
  // PCF85063 寄存器 0x04..0x0A：秒、分、时、日、周、月、年。
  Wire.beginTransmission(RTC_ADDR);
  Wire.write(0x04);
  if (Wire.endTransmission(false) != 0 || Wire.requestFrom((int)RTC_ADDR, 7) != 7) return false;
  uint8_t raw[7];
  for (uint8_t i = 0; i < 7; ++i) raw[i] = Wire.read();
  const int second = bcdToDec(raw[0] & 0x7F), minute = bcdToDec(raw[1] & 0x7F);
  const int hour = bcdToDec(raw[2] & 0x3F), day = bcdToDec(raw[3] & 0x3F);
  const int month = bcdToDec(raw[5] & 0x1F), year = bcdToDec(raw[6]);
  if (second > 59 || minute > 59 || hour > 23 || day < 1 || day > 31 || month < 1 || month > 12) return false;
  timeInfo = {};
  timeInfo.tm_sec = second; timeInfo.tm_min = minute; timeInfo.tm_hour = hour;
  timeInfo.tm_mday = day; timeInfo.tm_wday = bcdToDec(raw[4] & 0x07);
  timeInfo.tm_mon = month - 1; timeInfo.tm_year = 100 + year;
  return true;
}

void writeRtcTime(const struct tm& timeInfo) {
  Wire.beginTransmission(RTC_ADDR);
  Wire.write(0x04);
  Wire.write(decToBcd(timeInfo.tm_sec)); Wire.write(decToBcd(timeInfo.tm_min));
  Wire.write(decToBcd(timeInfo.tm_hour)); Wire.write(decToBcd(timeInfo.tm_mday));
  Wire.write(decToBcd(timeInfo.tm_wday)); Wire.write(decToBcd(timeInfo.tm_mon + 1));
  Wire.write(decToBcd((timeInfo.tm_year + 1900) % 100));
  if (Wire.endTransmission() == 0 && !rtcTrusted) {
    Preferences preferences;
    preferences.begin("dashboard", false);
    preferences.putBool("rtc_trusted", true);
    preferences.end();
    rtcTrusted = true;
  }
}

bool readDashboardTime(struct tm& timeInfo) {
  // A valid NTP clock is authoritative. It also makes RTC safe for later offline boots.
  if (getLocalTime(&timeInfo, 100)) {
    ntpReady = true;
    writeRtcTime(timeInfo);
    return true;
  }
  ntpReady = false;
  // A factory/uninitialised RTC must never overwrite a newly rendered page.
  return rtcTrusted && readRtcTime(timeInfo);
}

void drawLocalClock() {
  struct tm timeInfo;
  if (!readDashboardTime(timeInfo)) return;
  char clockText[6];
  strftime(clockText, sizeof(clockText), "%H:%M", &timeInfo);

  if (localHomeValid && lastClockText == clockText) return;
  lastClockText = clockText;
  // 仅清除和重绘大时间的区域；日期和其余主页保持不闪烁。
  display.setPartialWindow(20, 58, 360, 100);
  display.firstPage();
  do {
    display.fillRect(20, 58, 360, 100, GxEPD_WHITE);
    for (uint8_t i = 0; i < 5; ++i) {
      const ClockGlyph glyph = clockGlyphFor(clockText[i]);
      if (glyph.bits) {
        display.drawBitmap(24 + i * CLOCK_GLYPH_ADVANCE + glyph.xOffset,
                           150 + glyph.yOffset,
                           glyph.bits, glyph.width, glyph.height, GxEPD_BLACK);
      }
    }
  } while (display.nextPage());
}

void drawLocalDate() {
  struct tm timeInfo;
  if (!readDashboardTime(timeInfo)) return;
  char dateText[16];
  strftime(dateText, sizeof(dateText), "%Y-%m-%d", &timeInfo);
  if (localHomeValid && lastDateText == dateText) return;
  lastDateText = dateText;
  const int values[8] = {
    (timeInfo.tm_year + 1900) / 1000, ((timeInfo.tm_year + 1900) / 100) % 10,
    ((timeInfo.tm_year + 1900) / 10) % 10, (timeInfo.tm_year + 1900) % 10,
    (timeInfo.tm_mon + 1) / 10, (timeInfo.tm_mon + 1) % 10,
    timeInfo.tm_mday / 10, timeInfo.tm_mday % 10
  };
  // All date components are one bitmap font family: 2026年10月12日 · 星期一.
  display.setPartialWindow(20, 164, 420, 42);
  display.firstPage();
  do {
    display.fillRect(20, 164, 420, 42, GxEPD_WHITE);
    int x = 28;
    for (uint8_t i = 0; i < 4; ++i, x += DATE_DIGIT_W) {
      const uint8_t* digit = reinterpret_cast<const uint8_t*>(pgm_read_ptr(&date_digits[values[i]]));
      display.drawBitmap(x, 174, digit, DATE_DIGIT_W, DATE_GLYPH_H, GxEPD_BLACK);
    }
    display.drawBitmap(x, 174, date_unit_year, DATE_UNIT_W, DATE_UNIT_H, GxEPD_BLACK); x += DATE_UNIT_W;
    for (uint8_t i = 4; i < 6; ++i, x += DATE_DIGIT_W) {
      const uint8_t* digit = reinterpret_cast<const uint8_t*>(pgm_read_ptr(&date_digits[values[i]]));
      display.drawBitmap(x, 174, digit, DATE_DIGIT_W, DATE_GLYPH_H, GxEPD_BLACK);
    }
    display.drawBitmap(x, 174, date_unit_month, DATE_UNIT_W, DATE_UNIT_H, GxEPD_BLACK); x += DATE_UNIT_W;
    for (uint8_t i = 6; i < 8; ++i, x += DATE_DIGIT_W) {
      const uint8_t* digit = reinterpret_cast<const uint8_t*>(pgm_read_ptr(&date_digits[values[i]]));
      display.drawBitmap(x, 174, digit, DATE_DIGIT_W, DATE_GLYPH_H, GxEPD_BLACK);
    }
    display.drawBitmap(x, 174, date_unit_day, DATE_UNIT_W, DATE_UNIT_H, GxEPD_BLACK); x += DATE_UNIT_W + 4;
    display.drawBitmap(x, 174, date_separator_dot, 12, DATE_GLYPH_H, GxEPD_BLACK); x += 16;
    const uint8_t weekday = timeInfo.tm_wday % 7;
    const uint8_t* glyph = reinterpret_cast<const uint8_t*>(pgm_read_ptr(&weekday_glyphs[weekday]));
    display.drawBitmap(x, 174, glyph, WEEKDAY_GLYPH_W, WEEKDAY_GLYPH_H, GxEPD_BLACK);
  } while (display.nextPage());
}

bool readPmicRegister(uint8_t reg, uint8_t& value) {
  Wire.beginTransmission(PMIC_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0 || Wire.requestFrom((int)PMIC_ADDR, 1) != 1) return false;
  value = Wire.read();
  return true;
}

bool readBatteryTelemetry(uint8_t& percent, uint16_t& millivolts) {
  // AXP2101: 0xA4 is the fuel-gauge percentage; 0x34/0x35 expose the 14-bit VBAT ADC.
  uint8_t level = 0, high = 0, low = 0;
  if (!readPmicRegister(0xA4, level) || level > 100) return false;
  if (!readPmicRegister(0x34, high) || !readPmicRegister(0x35, low)) return false;
  percent = level;
  millivolts = (uint16_t(high & 0x3F) << 8) | low; // AXP2101 VBAT ADC: 1mV per count.
  return millivolts >= 2500 && millivolts <= 4600;
}

void drawWifiGlyph(int x, int y) {
  // 标准三层拱形 Wi-Fi 位图（无底部点）。
  display.drawBitmap(x, y, WIFI_ICON, 24, 18, GxEPD_BLACK);
}

const uint8_t BATTERY_TEXT_GLYPHS[12][5] PROGMEM = {
  {0x3e, 0x51, 0x49, 0x45, 0x3e},
  {0x0, 0x42, 0x7f, 0x40, 0x0},
  {0x72, 0x49, 0x49, 0x49, 0x46},
  {0x21, 0x41, 0x49, 0x4d, 0x33},
  {0x18, 0x14, 0x12, 0x7f, 0x10},
  {0x27, 0x45, 0x45, 0x45, 0x39},
  {0x3c, 0x4a, 0x49, 0x49, 0x31},
  {0x41, 0x21, 0x11, 0x9, 0x7},
  {0x36, 0x49, 0x49, 0x49, 0x36},
  {0x46, 0x49, 0x49, 0x29, 0x1e},
  {0x23, 0x13, 0x8, 0x64, 0x62},
  {0x8, 0x8, 0x8, 0x8, 0x8},
};

void drawBatteryGlyph(int x, int y, int percent) {
  display.drawRect(x, y, 29, 16, GxEPD_BLACK);
  display.fillRect(x + 29, y + 5, 4, 7, GxEPD_BLACK);
  if (percent < 0) return;
  const int fillWidth = max(1, min(25, (percent * 25 + 99) / 100));
  display.fillRect(x + 2, y + 2, fillWidth, 12, GxEPD_BLACK);
}

void updateLocalHomePanel() {
  if (currentPage != PAGE_HOME) return;
  // Clean the entire home page before the next group of partial writes.
  if (homePartialRefreshes >= MAX_HOME_PARTIAL_REFRESHES && pageCached[PAGE_HOME] && pageCache[PAGE_HOME]) {
    drawFrame(pageCache[PAGE_HOME], true);
    homePartialRefreshes = 0;
  }
  drawLocalClock();
  drawLocalDate();
  float tempC = 0.0f, humidity = 0.0f;
  const bool sensorOk = readShtc3(tempC, humidity);

  const String temperatureText = sensorOk ? String(tempC, 1) + " C" : "--.- C";
  const String humidityText = sensorOk ? String((int)(humidity + 0.5f)) + " %" : "-- %";
  if (!localHomeValid || temperatureText != lastTemperatureText || humidityText != lastHumidityText) {
  // 温湿度位于大时间右侧：不覆盖时间，且不跨越 x=480 的分割线。
  display.setPartialWindow(384, 52, 92, 76);
  display.firstPage();
  do {
    display.fillRect(384, 52, 92, 76, GxEPD_WHITE);
    display.setTextColor(GxEPD_BLACK);
    display.setTextSize(1);
    display.setTextSize(2);
    display.setCursor(390, 84);
    if (sensorOk) display.printf("%.1f C", tempC); else display.print("--.- C");
    display.setCursor(414, 112);
    if (sensorOk) display.printf("%d %%", (int)(humidity + 0.5f)); else display.print("-- %");
  } while (display.nextPage());

  lastTemperatureText = temperatureText;
  lastHumidityText = humidityText;
  }

  uint8_t batteryPercent = 0;
  uint16_t batteryMv = 0;
  const bool batteryOk = readBatteryTelemetry(batteryPercent, batteryMv);
  const String statusText = String((int)WiFi.status()) + ":" + (batteryOk ? String(batteryPercent) : "unknown");
  if (!localHomeValid || statusText != lastStatusText) {
  // Right status group: Wi-Fi, actual battery gauge, and real percentage.
  display.setPartialWindow(630, 20, 150, 42);
  display.firstPage();
  do {
    display.fillRect(630, 20, 150, 42, GxEPD_WHITE);
    drawWifiGlyph(638, 30);
    drawBatteryGlyph(682, 31, batteryOk ? batteryPercent : -1);
    char batteryLabel[8];
    if (batteryOk) snprintf(batteryLabel, sizeof(batteryLabel), "%u%%", batteryPercent);
    else snprintf(batteryLabel, sizeof(batteryLabel), "--%%");
    // Fixed 5x7 bitmap glyphs: visible pixels y=32..45, battery outline y=31..46.
    // Both centers are exactly y=38.5. No font/cursor state can move this label.
    for (uint8_t i = 0; batteryLabel[i]; ++i) {
      const char c = batteryLabel[i];
      const uint8_t glyphIndex = (c >= '0' && c <= '9') ? c - '0' : (c == '%' ? 10 : 11);
      for (uint8_t col = 0; col < 5; ++col) {
        const uint8_t bits = pgm_read_byte(&BATTERY_TEXT_GLYPHS[glyphIndex][col]);
        for (uint8_t row = 0; row < 7; ++row) {
          if (bits & (1 << row)) display.fillRect(724 + i * 12 + col * 2, 32 + row * 2, 2, 2, GxEPD_BLACK);
        }
      }
    }
  } while (display.nextPage());
  lastStatusText = statusText;
  }
  localHomeValid = true;
  ++homePartialRefreshes;
}

uint32_t refreshIntervalFor(Page page) {
  switch (page) {
    case PAGE_HOME: return HOME_REFRESH_MS;
    case PAGE_STOCK: return STOCK_REFRESH_MS;
    case PAGE_READING: return READING_REFRESH_MS;
    default: return HOME_REFRESH_MS;
  }
}

void connectWifi() {
  // Connection and NTP setup stay in the background; button handling never blocks on Wi-Fi.
  if (!wifiConfigured()) return;
  if (WiFi.status() == WL_CONNECTED) {
    if (!ntpRequested) {
      configTime(8 * 3600, 0, "ntp.aliyun.com", "pool.ntp.org");
      ntpRequested = true;
    }
    return;
  }
  if (lastWifiAttempt && millis() - lastWifiAttempt < 15000) return;
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(true); // Modem power saving; retain association and immediate button response.
  WiFi.begin(wifiSsid.c_str(), wifiPassword.c_str());
  lastWifiAttempt = millis();
  ntpRequested = false;
}

const char* endpointFor(Page page) {
  switch (page) {
    case PAGE_HOME: return "/home.bin";
    case PAGE_STOCK: return "/screen.bin";
    case PAGE_READING: return "/reading.bin";
    default: return nullptr;
  }
}

FetchResult fetchCurrentPage(bool forceDownload) {
  const char* endpoint = endpointFor(currentPage);
  if (!endpoint || WiFi.status() != WL_CONNECTED) return FETCH_FAILED;

  // 无手工地址时，每次请求前都可重新发现电脑服务；电脑重启或换 IP 后可自愈。
  if (!dashboardHost.length() && !discoverAgent()) return FETCH_FAILED;
  if (!hostConfigured()) return FETCH_FAILED;

  const uint8_t slot = static_cast<uint8_t>(currentPage);
  HTTPClient http;
  const String base = dashboardHost.length() ? dashboardHost : discoveredHost;
  http.begin(base + "/api/display/page/" + String(static_cast<uint8_t>(currentPage) + 1));
  http.setTimeout(2500);  // 局域网服务不可达时，不让按键看起来卡死。
  const char* headers[] = {"ETag"};
  http.collectHeaders(headers, 1);
  if (!forceDownload && etags[slot].length()) {
    http.addHeader("If-None-Match", etags[slot]);
  }

  const int status = http.GET();
  if (status == HTTP_CODE_NOT_MODIFIED) {
    http.end();
    return UNCHANGED;
  }
  if (status != HTTP_CODE_OK) {
    http.end();
    return FETCH_FAILED;
  }

  WiFiClient* stream = http.getStreamPtr();
  size_t received = 0;
  const uint32_t started = millis();
  while (received < FRAME_BYTES && millis() - started < 3500) {
    const int available = stream->available();
    if (available > 0) {
      const size_t wanted = min((size_t)available, FRAME_BYTES - received);
      const int read = stream->readBytes(framebuf + received, wanted);
      if (read <= 0) break;
      received += read;
    } else if (!stream->connected()) {
      break;
    } else {
      delay(2);
    }
  }
  if (received == FRAME_BYTES) {
    etags[slot] = http.header("ETag");
    http.end();
    return FETCHED;
  }
  http.end();
  return FETCH_FAILED;
}

void refreshCurrentPage(bool pageChanged, bool forceFullClean) {
  const uint8_t slot = static_cast<uint8_t>(currentPage);
  // 切页先显示板端缓存；网络请求在后面进行，失败也不会覆盖当前有效画面。
  if (pageChanged && pageCached[slot] && pageCache[slot]) {
    drawFrame(pageCache[slot], true);
    unchangedPageChecks[slot] = 0;
    if (currentPage == PAGE_HOME) homePartialRefreshes = 0;
  }

  connectWifi();
  const FetchResult result = fetchCurrentPage(pageChanged);
  if (result == FETCHED) {
    // 将每页最后一次成功下载的版本存入 PSRAM，作为切页/断网兜底。
    if (pageCache[slot]) {
      memcpy(pageCache[slot], framebuf, FRAME_BYTES);
      pageCached[slot] = true;
    }
    drawRawFrame(true);
    unchangedPageChecks[slot] = 0;
    if (currentPage == PAGE_HOME) homePartialRefreshes = 0;
  } else if (result == UNCHANGED) {
    // A page whose source did not change still gets an occasional full-clean waveform.
    if (++unchangedPageChecks[slot] >= MAX_UNCHANGED_PAGE_CHECKS && pageCached[slot] && pageCache[slot]) {
      drawFrame(pageCache[slot], true);
      unchangedPageChecks[slot] = 0;
      if (currentPage == PAGE_HOME) homePartialRefreshes = 0;
    }
  } else if (forceFullClean && pageCached[slot] && pageCache[slot]) {
    // Network unavailable: scheduled cache redraw prevents ghosting without blanking the screen.
    drawFrame(pageCache[slot], true);
    unchangedPageChecks[slot] = 0;
    if (currentPage == PAGE_HOME) homePartialRefreshes = 0;
  }
  // 请求失败时保留已显示的缓存，不再绘制英文错误页。
  if (currentPage == PAGE_HOME) updateLocalHomePanel();
  lastRemoteRefresh = millis();
  lastRemotePage = currentPage;
}

uint32_t buttonPressDuration(int pin) {
  if (digitalRead(pin) != LOW) return false;
  delay(25);
  if (digitalRead(pin) != LOW) return 0;
  const uint32_t started = millis();
  while (digitalRead(pin) == LOW) delay(10);
  return millis() - started;
}

void handleButtons() {
  const uint32_t functionDuration = buttonPressDuration(BTN_FUNCTION);
  if (functionDuration >= 3000) {
    startConfigPortal();
  } else if (functionDuration > 0) {
    refreshCurrentPage(false);
  } else if (buttonPressDuration(BTN_UP) > 0) {
    currentPage = static_cast<Page>((currentPage + PAGE_COUNT - 1) % PAGE_COUNT);
    refreshCurrentPage(true);
  } else if (buttonPressDuration(BTN_DOWN) > 0) {
    currentPage = static_cast<Page>((currentPage + 1) % PAGE_COUNT);
    refreshCurrentPage(true);
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);

  pinMode(BTN_UP, INPUT_PULLUP);
  pinMode(BTN_FUNCTION, INPUT_PULLUP);
  pinMode(BTN_DOWN, INPUT_PULLUP);
  Wire.begin(SHT_SDA, SHT_SCL);
  loadStoredSettings();

  SPI.begin(EPD_SCLK, -1, EPD_MOSI, EPD_CS);
  display.init(115200);
  display.setRotation(2);  // 设备实际摆放方向：整屏旋转 180°
  for (uint8_t i = 0; i < PAGE_COUNT; ++i) {
    pageCache[i] = static_cast<uint8_t*>(ps_malloc(FRAME_BYTES));
  }
  // 首次开机若网络或电脑服务尚未就绪，先显示内置主页，绝不留下白屏。
  static_assert(HOME_FALLBACK_BITMAP_LEN == FRAME_BYTES, "主页兜底位图尺寸错误");
  if (pageCache[PAGE_HOME]) {
    memcpy(pageCache[PAGE_HOME], home_fallback_bitmap, FRAME_BYTES);
    pageCached[PAGE_HOME] = true;
  }
  if (pageCache[PAGE_STOCK]) {
    memcpy(pageCache[PAGE_STOCK], stock_screen_bitmap, FRAME_BYTES);
    pageCached[PAGE_STOCK] = true;
  }
  if (pageCache[PAGE_READING]) {
    memcpy(pageCache[PAGE_READING], reading_fallback_bitmap, FRAME_BYTES);
    pageCached[PAGE_READING] = true;
  }
  drawFrame(home_fallback_bitmap, true);
  refreshCurrentPage(true);
  lastLocalRefresh = millis();
  lastRemoteRefresh = millis();
}

void loop() {
  connectWifi();  // Start time sync as soon as the network joins, independent of page refresh.
  handleButtons();
  const uint32_t now = millis();
  if (currentPage == PAGE_HOME && now - lastLocalRefresh >= LOCAL_REFRESH_MS) {
    updateLocalHomePanel();
    lastLocalRefresh = now;
  }
  if (lastRemotePage != currentPage || now - lastRemoteRefresh >= refreshIntervalFor(currentPage)) {
    // 主页的远程检查周期同时承担整屏清洁刷新，避免分钟级局刷产生横纹残影。
    refreshCurrentPage(false, true);
  }
  delay(50);
}
