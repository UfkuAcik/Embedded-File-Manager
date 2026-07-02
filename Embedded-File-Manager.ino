#include <Arduino.h>
#include <SPI.h>
#include <SD.h> 
#include <esp_rom_crc.h>


// PSRAM / Buffer Settings
uint8_t* transferBuffer = nullptr;
size_t transferBufferSize = 4096; // Chunk size for UART

uint8_t* cacheBuffer = nullptr;
size_t cacheBufferSize = 0;       // Massive cache to bypass SD card latency



// ==========================================
// CONFIGURATION
// ==========================================
// SD Card Pins
#define SD_CS    13
#define SD_MOSI  12
#define SD_MISO  35
#define SD_SCK   14

// WiFi Settings
const char* WIFI_SSID = "Your_WiFi";
const char* WIFI_PASS = "password";
#define WIFI_PORT 8080

// Bluetooth Settings
#define BT_DEVICE_NAME "ESP32_Storage"
// ==========================================

#include <WiFi.h>
#include <BluetoothSerial.h>

BluetoothSerial SerialBT;
WiFiServer server(WIFI_PORT);
WiFiClient wifiClient;

void setup() {
    Serial.setRxBufferSize(32768); 
    Serial.begin(921600);

    while (!Serial) { ; }
    
    // Aggressive PSRAM Allocation
    if (psramFound()) {
        transferBufferSize = 32768; // UART chunk size 32KB
        transferBuffer = (uint8_t*)heap_caps_malloc(transferBufferSize, MALLOC_CAP_SPIRAM);
        
        // Allocate a massive cache (e.g., 4MB) to prevent SD card delays
        size_t tryCacheSize = 4 * 1024 * 1024; 
        cacheBuffer = (uint8_t*)heap_caps_malloc(tryCacheSize, MALLOC_CAP_SPIRAM);
        if (cacheBuffer) {
            cacheBufferSize = tryCacheSize;
        } else {
            // Try 2MB if 4MB fails
            tryCacheSize = 2 * 1024 * 1024;
            cacheBuffer = (uint8_t*)heap_caps_malloc(tryCacheSize, MALLOC_CAP_SPIRAM);
            if (cacheBuffer) {
                cacheBufferSize = tryCacheSize;
            }
        }
    } 
    
    // Standard RAM usage if PSRAM is not available
    if (transferBuffer == nullptr) {
        transferBufferSize = 4096;
        transferBuffer = (uint8_t*)malloc(transferBufferSize);
        cacheBuffer = nullptr; 
        cacheBufferSize = 0;
    }

    if (transferBuffer == nullptr) {
        Serial.println("ERROR: Buffer allocation failed!");
        while (1) { delay(100); }
    }

    SPI.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);
    if (!SD.begin(SD_CS, SPI, 20000000)) { 
        Serial.println("ERROR: SD Card initialization failed!");
        while (1) { delay(100); }
    }

    Serial.println("READY");
    Serial.print("BUFFER_SIZE:");
    Serial.println(transferBufferSize);

    // Initialize Bluetooth
    SerialBT.begin(BT_DEVICE_NAME);
    Serial.println("Bluetooth Started: " + String(BT_DEVICE_NAME));

    // Initialize WiFi
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    Serial.print("Connecting to WiFi");
    int retries = 0;
    while (WiFi.status() != WL_CONNECTED && retries < 20) {
        delay(500);
        Serial.print(".");
        retries++;
    }
    Serial.println();
    if (WiFi.status() == WL_CONNECTED) {
        Serial.print("WiFi Connected! IP Address: ");
        Serial.println(WiFi.localIP());
        server.begin();
    } else {
        Serial.println("WiFi Connection Failed.");
    }
}

void handleGetDir(Stream* comms, String path) {
    File dir = SD.open(path);
    if (!dir || !dir.isDirectory()) {
        comms->println("ERROR: Not a directory");
        return;
    }
    comms->println("ACK");
    File file = dir.openNextFile();
    while (file) {
        time_t t = file.getLastWrite();
        if (file.isDirectory()) {
            comms->printf("DIR:%s:0:%ld\n", file.name(), (long)t);
        } else {
            comms->printf("FILE:%s:%u:%ld\n", file.name(), (unsigned int)file.size(), (long)t);
        }
        file.close();
        file = dir.openNextFile();
    }
    comms->println("END");
    comms->flush();
    dir.close();
}

void handleDiskInfo(Stream* comms) {
    uint64_t totalBytes = SD.totalBytes();
    uint64_t usedBytes = SD.usedBytes();
    
    if (totalBytes == 0) {
        totalBytes = SD.cardSize();
    }

    uint32_t totalMB = (uint32_t)(totalBytes / (1024 * 1024));
    uint32_t usedMB = (uint32_t)(usedBytes / (1024 * 1024));

    comms->println("ACK");
    comms->print("TOTAL:");
    comms->println(totalMB);
    comms->print("USED:");
    comms->println(usedMB);
    comms->println("END");
    comms->flush();
}

void handleMkdir(Stream* comms, String path) {
    if (SD.mkdir(path)) {
        comms->println("ACK");
    } else {
        comms->println("ERROR: Mkdir failed");
    }
    comms->flush();
}

void handleRemove(Stream* comms, String path) {
    File f = SD.open(path);
    if (!f) {
        comms->println("ERROR: File not found");
        return;
    }
    bool isDir = f.isDirectory();
    f.close();

    bool success = false;
    if (isDir) {
        success = SD.rmdir(path);
    } else {
        success = SD.remove(path);
    }

    if (success) {
        comms->println("ACK");
    } else {
        comms->println("ERROR: Remove failed");
    }
    comms->flush();
}

void handleRename(Stream* comms, String oldPath, String newPath) {
    if (SD.rename(oldPath, newPath)) {
        comms->println("ACK");
    } else {
        comms->println("ERROR: Rename failed");
    }
    comms->flush();
}

void handleCopy(Stream* comms, String sourcePath, String destPath) {
    File srcFile = SD.open(sourcePath, FILE_READ);
    if (!srcFile) {
        comms->println("ERROR: Source file not found");
        comms->flush();
        return;
    }

    File destFile = SD.open(destPath, FILE_WRITE);
    if (!destFile) {
        comms->println("ERROR: Destination file create failed");
        srcFile.close();
        comms->flush();
        return;
    }

    size_t total = srcFile.size();
    size_t copied = 0;
    unsigned long lastReport = 0;

    while (srcFile.available()) {
        size_t bytesRead = srcFile.read(transferBuffer, transferBufferSize);
        if (bytesRead > 0) {
            destFile.write(transferBuffer, bytesRead);
            copied += bytesRead;
            unsigned long now = millis();
            if (now - lastReport > 200 || copied == total) {
                comms->printf("PROG:%u:%u\n", (unsigned int)copied, (unsigned int)total);
                lastReport = now;
            }
        }
    }

    srcFile.close();
    destFile.close();
    comms->println("ACK");
    comms->flush();
}

void handleDownload(Stream* comms, String path) {
    File file = SD.open(path, FILE_READ);
    if (!file) {
        comms->println("ERROR: File open failed");
        return;
    }

    comms->print("ACK ");
    comms->println(file.size());

    size_t cacheOccupied = 0;
    size_t cachePos = 0;

    while (file.available() || cachePos < cacheOccupied) {
        
        // Refill Cache if empty and SD card still has data
        if (cacheBuffer != nullptr && cachePos >= cacheOccupied && file.available()) {
            cacheOccupied = file.read(cacheBuffer, cacheBufferSize);
            cachePos = 0;
        }

        size_t bytesToSend = 0;
        if (cacheBuffer != nullptr) {
            bytesToSend = min(transferBufferSize, cacheOccupied - cachePos);
            memcpy(transferBuffer, cacheBuffer + cachePos, bytesToSend);
            cachePos += bytesToSend;
        } else {
            // Read directly from SD if no PSRAM
            bytesToSend = file.read(transferBuffer, transferBufferSize);
        }

        if (bytesToSend > 0) {
            uint32_t crc = esp_rom_crc32_le(0, transferBuffer, bytesToSend);
            
            uint8_t header[2];
            header[0] = (bytesToSend >> 8) & 0xFF;
            header[1] = bytesToSend & 0xFF;
            comms->write(header, 2);
            comms->write(transferBuffer, bytesToSend);
            
            uint8_t crcBytes[4];
            crcBytes[0] = (crc >> 24) & 0xFF;
            crcBytes[1] = (crc >> 16) & 0xFF;
            crcBytes[2] = (crc >> 8) & 0xFF;
            crcBytes[3] = crc & 0xFF;
            comms->write(crcBytes, 4);

            bool acked = false;
            while (!acked) {
                String resp = comms->readStringUntil('\n');
                resp.trim();
                if (resp == "ACK") {
                    acked = true;
                } else if (resp == "NACK") {
                    comms->write(header, 2);
                    comms->write(transferBuffer, bytesToSend);
                    comms->write(crcBytes, 4);
                } else {
                    file.close();
                    return;
                }
            }
        }
    }

    uint8_t eof[2] = {0, 0};
    comms->write(eof, 2);
    comms->flush();
    file.close();
}

void handleUpload(Stream* comms, String path, size_t fileSize) {
    File file = SD.open(path, FILE_WRITE);
    if (!file) {
        comms->println("ERROR: File create failed");
        return;
    }

    comms->print("ACK ");
    comms->println(transferBufferSize); 

    size_t bytesReceivedTotal = 0;
    size_t cacheOccupied = 0;

    while (bytesReceivedTotal < fileSize) {
        while (comms->available() < 2) { delay(1); }
        uint8_t header[2];
        comms->readBytes(header, 2);
        uint16_t chunkLen = (header[0] << 8) | header[1];

        if (chunkLen == 0) break;

        if (chunkLen > transferBufferSize) {
            comms->println("NACK_FATAL: Chunk too large");
            file.close();
            return;
        }

        size_t received = 0;
        uint32_t startTime = millis();
        while (received < chunkLen) {
            if (comms->available()) {
                received += comms->readBytes(transferBuffer + received, chunkLen - received);
            }
            if (millis() - startTime > 5000) { 
                comms->println("NACK_FATAL: Timeout");
                file.close();
                return;
            }
        }

        while (comms->available() < 4) { yield(); }
        uint8_t crcBytes[4];
        comms->readBytes(crcBytes, 4);
        uint32_t receivedCrc = ((uint32_t)crcBytes[0] << 24) | ((uint32_t)crcBytes[1] << 16) | ((uint32_t)crcBytes[2] << 8) | crcBytes[3];

        uint32_t calculatedCrc = esp_rom_crc32_le(0, transferBuffer, chunkLen);
        if (calculatedCrc == receivedCrc) {
            
            // Aggressive PSRAM Caching
            if (cacheBuffer != nullptr) {
                if (cacheOccupied + chunkLen <= cacheBufferSize) {
                    memcpy(cacheBuffer + cacheOccupied, transferBuffer, chunkLen);
                    cacheOccupied += chunkLen;
                } else {
                    // Cache is full! Flush everything to SD and write new data to cache
                    file.write(cacheBuffer, cacheOccupied);
                    memcpy(cacheBuffer, transferBuffer, chunkLen);
                    cacheOccupied = chunkLen;
                }
            } else {
                // Write directly if no PSRAM
                file.write(transferBuffer, chunkLen);
            }
            
            bytesReceivedTotal += chunkLen;
            comms->println("ACK");
        } else {
            comms->println("NACK");
        }
    }

    // Flush remaining data in cache to SD when upload finishes
    if (cacheBuffer != nullptr && cacheOccupied > 0) {
        file.write(cacheBuffer, cacheOccupied);
    }

    file.close();
    
    // Clear any lingering garbage in the RX buffer before signaling Python
    while(comms->available()) { comms->read(); }
    
    comms->println("UPLOAD_DONE");
    comms->flush();
}

void loop() {
    Stream* comms = nullptr;

    if (Serial.available()) {
        comms = &Serial;
    } else if (SerialBT.available()) {
        comms = &SerialBT;
    } else {
        WiFiClient newClient = server.available();
        if (newClient) {
            if (wifiClient) wifiClient.stop();
            wifiClient = newClient;
            Serial.println("New WiFi Client connected.");
        }
        if (wifiClient.connected() && wifiClient.available()) {
            comms = &wifiClient;
        }
    }

    if (comms) {
        String cmdLine = comms->readStringUntil('\n');
        cmdLine.trim();
        if (cmdLine.length() == 0) return;

        if (cmdLine.startsWith("GET_DIR ")) {
            handleGetDir(comms, cmdLine.substring(8));
        } else if (cmdLine == "DISK_INFO") {
            handleDiskInfo(comms);
        } else if (cmdLine.startsWith("MKDIR ")) {
            handleMkdir(comms, cmdLine.substring(6));
        } else if (cmdLine.startsWith("DELETE ")) {
            handleRemove(comms, cmdLine.substring(7));
        } else if (cmdLine.startsWith("RENAME ")) {
            String args = cmdLine.substring(7);
            int pipeIdx = args.indexOf('|');
            if (pipeIdx != -1) {
                handleRename(comms, args.substring(0, pipeIdx), args.substring(pipeIdx + 1));
            } else {
                comms->println("ERROR: Invalid RENAME arguments");
            }
        } else if (cmdLine.startsWith("COPY ")) {
            String args = cmdLine.substring(5);
            int pipeIdx = args.indexOf('|');
            if (pipeIdx != -1) {
                handleCopy(comms, args.substring(0, pipeIdx), args.substring(pipeIdx + 1));
            } else {
                comms->println("ERROR: Invalid COPY arguments");
            }
        } else if (cmdLine.startsWith("DOWNLOAD ")) {
            handleDownload(comms, cmdLine.substring(9));
        } else if (cmdLine.startsWith("UPLOAD ")) {
            String args = cmdLine.substring(7);
            int pipeIdx = args.indexOf('|');
            if (pipeIdx != -1) {
                handleUpload(comms, args.substring(0, pipeIdx), args.substring(pipeIdx + 1).toInt());
            } else {
                comms->println("ERROR: Invalid UPLOAD arguments");
            }
        } else if (cmdLine == "ECHO") {
            comms->println("ECHO_OK");
            comms->flush();
        } else {
            comms->println("ERROR: Unknown command: [" + cmdLine + "]");
        }
    }
}
