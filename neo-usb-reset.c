#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/usb/IOUSBLib.h>
#include <IOKit/pwr_mgt/IOPMLib.h>
#include <IOKit/IOMessage.h>
#include <mach/mach_error.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static io_connect_t power_port;
static void power_event(void *ref, io_service_t service, natural_t message, void *arg) {
    (void)ref; (void)service;
    if (message == kIOMessageCanSystemSleep || message == kIOMessageSystemWillSleep) {
        if (message == kIOMessageSystemWillSleep) { puts("sleep"); fflush(stdout); }
        IOAllowPowerChange(power_port, (long)arg);
    }
    if (message == kIOMessageSystemHasPoweredOn) { puts("wake"); fflush(stdout); }
}

static int watch_wake(void) {
    IONotificationPortRef port = NULL;
    io_object_t notifier = 0;
    power_port = IORegisterForSystemPower(NULL, &port, power_event, &notifier);
    if (!power_port || !port) { fprintf(stderr, "Cannot register power notifications\n"); return 1; }
    CFRunLoopAddSource(CFRunLoopGetCurrent(), IONotificationPortGetRunLoopSource(port), kCFRunLoopDefaultMode);
    puts("ready"); fflush(stdout);
    CFRunLoopRun();
    IODeregisterForSystemPower(&notifier);
    IOServiceClose(power_port);
    IONotificationPortDestroy(port);
    return 0;
}

static int number(io_service_t service, CFStringRef key) {
    CFTypeRef value = IORegistryEntryCreateCFProperty(service, key, kCFAllocatorDefault, 0);
    int result = -1;
    if (value && CFGetTypeID(value) == CFNumberGetTypeID())
        CFNumberGetValue(value, kCFNumberIntType, &result);
    if (value) CFRelease(value);
    return result;
}

static void json_string(const char *s) {
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        if (*p == '"' || *p == '\\') printf("\\%c", *p);
        else if (*p < 32 || *p >= 127) printf("\\u%04x", *p);
        else putchar(*p);
    }
    putchar('"');
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--watch-wake") == 0) return watch_wake();
    int reset = argc == 5 && strcmp(argv[1], "--reset") == 0 &&
        strcmp(argv[4], "--confirm-no-firmware-update") == 0;
    int access_check = argc == 4 && strcmp(argv[1], "--access-check") == 0;
    int selected = reset || access_check;
    int product = selected && strcmp(argv[2], "0080") == 0 ? 0x0080 :
                  selected && strcmp(argv[2], "009A") == 0 ? 0x009a : -1;
    if ((selected && (product == -1 || !argv[3][0])) ||
        (!selected && !(argc == 2 && strcmp(argv[1], "--list") == 0))) {
        fprintf(stderr, "Usage: %s --list | --watch-wake | --access-check 0080|009A SERIAL | --reset 0080|009A SERIAL --confirm-no-firmware-update\n", argv[0]);
        return 2;
    }
    io_iterator_t iterator = 0;
    IOReturn result = IOServiceGetMatchingServices(kIOMainPortDefault,
        IOServiceMatching("IOUSBHostDevice"), &iterator);
    if (result != kIOReturnSuccess) { fprintf(stderr, "USB enumeration failed\n"); return 1; }
    io_service_t target = 0, service;
    int matches = 0;
    while ((service = IOIteratorNext(iterator))) {
        int pid = number(service, CFSTR("idProduct"));
        if (number(service, CFSTR("idVendor")) == 0x0fd9 && (pid == 0x0080 || pid == 0x009a)) {
            char serial[256] = "";
            CFTypeRef value = IORegistryEntryCreateCFProperty(service,
                CFSTR("USB Serial Number"), kCFAllocatorDefault, 0);
            if (value && CFGetTypeID(value) == CFStringGetTypeID() &&
                !CFStringGetCString(value, serial, sizeof(serial), kCFStringEncodingUTF8)) serial[0] = 0;
            if (value) CFRelease(value);
            uint64_t entry = 0;
            IORegistryEntryGetRegistryEntryID(service, &entry);
            if (!selected) {
                printf("{\"vendor\":\"0FD9\",\"product\":\"%04X\",\"model\":\"%s\",\"serial\":", pid,
                    pid == 0x0080 ? "Stream Deck MK.2" : "Stream Deck Neo");
                json_string(serial);
                printf(",\"location\":\"%08x\",\"entry\":%llu}\n",
                    number(service, CFSTR("locationID")), (unsigned long long)entry);
            }
            if (selected && pid == product && serial[0] && strcmp(serial, argv[3]) == 0) {
                matches++;
                if (!target) { target = service; IOObjectRetain(target); }
            }
        }
        IOObjectRelease(service);
    }
    IOObjectRelease(iterator);
    if (!selected) return 0;
    if (matches != 1) {
        fprintf(stderr, "Expected exactly one device with requested VID/PID/serial; found %d. No reset performed.\n", matches);
        if (target) IOObjectRelease(target);
        return 1;
    }
    IOCFPlugInInterface **plugin = NULL;
    IOUSBDeviceInterface500 **device = NULL;
    SInt32 score = 0;
    result = IOCreatePlugInInterfaceForService(target, kIOUSBDeviceUserClientTypeID,
        kIOCFPlugInInterfaceID, &plugin, &score);
    IOObjectRelease(target);
    if (result != kIOReturnSuccess || !plugin) {
        fprintf(stderr, "Create USB interface: 0x%08x (%s)\n", result, mach_error_string(result));
        return 1;
    }
    HRESULT query = (*plugin)->QueryInterface(plugin,
        CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID500), (LPVOID *)&device);
    (*plugin)->Release(plugin);
    if (query || !device) { fprintf(stderr, "QueryInterface failed: %d\n", (int)query); return 1; }
    if (access_check) {
        puts("USB user interface is available. No open or reset was performed.");
        (*device)->Release(device);
        return 0;
    }
    result = (*device)->USBDeviceOpen(device);
    if (result == kIOReturnSuccess) {
        result = (*device)->USBDeviceReEnumerate(device, 0);
        printf("USBDeviceReEnumerate: 0x%08x (%s)\n", result, mach_error_string(result));
        (*device)->USBDeviceClose(device);
    } else {
        fprintf(stderr, "USBDeviceOpen: 0x%08x (%s). Quit Stream Deck before a manual retry.\n", result, mach_error_string(result));
    }
    (*device)->Release(device);
    return result == kIOReturnSuccess ? 0 : 1;
}
