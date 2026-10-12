import Foundation
import Security

// Read or save an opaque credential in the login Keychain. Values travel over
// stdin/stdout, never through process arguments or security(1)'s line parser.
guard CommandLine.arguments.count == 3 else {
    fputs("Expected an operation and Keychain service name\n", stderr)
    exit(2)
}
let operation = CommandLine.arguments[1]
let service = CommandLine.arguments[2]
guard service.hasPrefix("Switchboard-v2-") else {
    fputs("Unsupported Keychain service\n", stderr)
    exit(2)
}
let account = NSUserName()
let secret = operation == "write" ? FileHandle.standardInput.readDataToEndOfFile() : Data()
if operation == "write" && secret.isEmpty {
    fputs("Empty credential\n", stderr)
    exit(2)
}

var result: OSStatus = errSecItemNotFound
service.withCString { servicePointer in
    account.withCString { accountPointer in
        var existing: SecKeychainItem?
        var passwordLength: UInt32 = 0
        var passwordData: UnsafeMutableRawPointer?
        if operation == "read" {
            result = SecKeychainFindGenericPassword(
                nil, UInt32(strlen(servicePointer)), servicePointer,
                UInt32(strlen(accountPointer)), accountPointer,
                &passwordLength, &passwordData, &existing
            )
        } else {
            result = SecKeychainFindGenericPassword(
                nil, UInt32(strlen(servicePointer)), servicePointer,
                UInt32(strlen(accountPointer)), accountPointer,
                nil, nil, &existing
            )
        }
        if operation == "read", result == errSecSuccess, let passwordData {
            let data = Data(bytes: passwordData, count: Int(passwordLength))
            FileHandle.standardOutput.write(data)
            SecKeychainItemFreeContent(nil, passwordData)
        } else if operation == "delete", result == errSecSuccess, let existing {
            result = SecKeychainItemDelete(existing)
        } else if operation == "delete", result == errSecItemNotFound {
            result = errSecSuccess
        } else if operation == "write", result == errSecSuccess, let existing {
            result = secret.withUnsafeBytes { bytes in
                SecKeychainItemModifyAttributesAndData(
                    existing, nil, UInt32(secret.count), bytes.baseAddress
                )
            }
        } else if operation == "write", result == errSecItemNotFound {
            result = secret.withUnsafeBytes { bytes in
                SecKeychainAddGenericPassword(
                    nil, UInt32(strlen(servicePointer)), servicePointer,
                    UInt32(strlen(accountPointer)), accountPointer,
                    UInt32(secret.count), bytes.baseAddress!, nil
                )
            }
        } else if operation != "read" && operation != "write" && operation != "delete" {
            result = errSecParam
        }
    }
}
if result != errSecSuccess {
    fputs("Keychain \(operation) failed (OSStatus \(result))\n", stderr)
    exit(1)
}
