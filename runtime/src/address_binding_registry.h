#pragma once

// Live GPU address bindings (VK_EXT_device_address_binding_report), kept so a VK_EXT_device_fault address can be
// traced back to the buffer, image or memory object that owns it. Diagnostic only, off unless
// LEONIDA_ADDRESS_BINDINGS=1. Standard library only: nothing in here calls Vulkan, so the unit test runs anywhere.

#include <cinttypes>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <map>
#include <mutex>
#include <string>
#include <utility>

namespace leonida {

struct AddressBinding {
    std::uint32_t object_type = 0;  // a VkObjectType value
    std::uint64_t handle = 0;
    std::uint64_t base = 0;
    std::uint64_t size = 0;
    std::uint64_t end() const { return base + size; }
};

struct AddressLookup {
    enum class Kind { none, inside, below, above };
    Kind kind = Kind::none;
    AddressBinding binding{};
    std::uint64_t offset = 0;    // inside: address - base
    std::uint64_t distance = 0;  // below: address - end; above: base - address
};

inline const char* object_type_name(std::uint32_t type) {
    switch (type) {
    case 1: return "Instance";
    case 3: return "Device";
    case 8: return "DeviceMemory";
    case 9: return "Buffer";
    case 10: return "Image";
    case 13: return "BufferView";
    case 14: return "ImageView";
    case 19: return "Pipeline";
    case 1000150000: return "AccelerationStructureKHR";
    case 1000165000: return "AccelerationStructureNV";
    default: return "Object";
    }
}

class AddressBindingRegistry {
public:
    // A bind of a range an object already holds at the same base replaces it; overlapping ranges coexist.
    void bind(std::uint32_t object_type, std::uint64_t handle, std::uint64_t base, std::uint64_t size) {
        std::lock_guard lock(mutex_);
        live_[{base, handle}] = {object_type, handle, base, size};
        ++binds_;
    }

    // Removes the binding with this base and size (and this handle, unless it is 0). False when none matches.
    bool unbind(std::uint64_t handle, std::uint64_t base, std::uint64_t size) {
        std::lock_guard lock(mutex_);
        for (auto it = live_.lower_bound({base, 0}); it != live_.end() && it->first.first == base; ++it) {
            if (it->second.size != size || (handle && it->first.second != handle)) continue;
            live_.erase(it);
            ++unbinds_;
            return true;
        }
        return false;
    }

    // Linear in the live bindings: meant for the fault report, not a hot path.
    AddressLookup lookup(std::uint64_t address) const {
        std::lock_guard lock(mutex_);
        bool have_below = false, have_above = false;
        std::uint64_t below = 0, above = 0;
        AddressBinding below_binding, above_binding;
        for (const auto& entry : live_) {
            const AddressBinding& binding = entry.second;
            if (address >= binding.base && address - binding.base < binding.size)
                return {AddressLookup::Kind::inside, binding, address - binding.base, 0};
            if (binding.base > address) {
                if (!have_above || binding.base - address < above) {
                    have_above = true;
                    above = binding.base - address;
                    above_binding = binding;
                }
            } else if (!have_below || address - binding.end() < below) {
                have_below = true;
                below = address - binding.end();
                below_binding = binding;
            }
        }
        if (have_below && (!have_above || below <= above)) return {AddressLookup::Kind::below, below_binding, 0, below};
        if (have_above) return {AddressLookup::Kind::above, above_binding, 0, above};
        return {};
    }

    std::size_t size() const { std::lock_guard lock(mutex_); return live_.size(); }
    std::uint64_t binds() const { std::lock_guard lock(mutex_); return binds_; }
    std::uint64_t unbinds() const { std::lock_guard lock(mutex_); return unbinds_; }

    // Set by the layer when LEONIDA_ADDRESS_BINDINGS=1 got the extension; the fault report prints only then.
    void set_enabled(bool on) { std::lock_guard lock(mutex_); enabled_ = on; }
    bool enabled() const { std::lock_guard lock(mutex_); return enabled_; }

private:
    mutable std::mutex mutex_;
    std::map<std::pair<std::uint64_t, std::uint64_t>, AddressBinding> live_;  // keyed by (base, handle)
    std::uint64_t binds_ = 0, unbinds_ = 0;
    bool enabled_ = false;
};

inline AddressBindingRegistry& address_binding_registry() {
    static AddressBindingRegistry registry;
    return registry;
}

// "inside Image 0x.. [0x.., 0x..) +0x.." or "nearest below Buffer 0x.. [0x.., 0x..) distance 0x..".
inline std::string describe_address(const AddressLookup& hit) {
    if (hit.kind == AddressLookup::Kind::none) return "no binding known";
    char text[256];
    const AddressBinding& b = hit.binding;
    const bool inside = hit.kind == AddressLookup::Kind::inside;
    std::snprintf(text, sizeof(text), "%s %s 0x%" PRIx64 " [0x%" PRIx64 ", 0x%" PRIx64 ") %s0x%" PRIx64,
                  inside ? "inside" : hit.kind == AddressLookup::Kind::below ? "nearest below" : "nearest above",
                  object_type_name(b.object_type), b.handle, b.base, b.end(), inside ? "+" : "distance ",
                  inside ? hit.offset : hit.distance);
    return text;
}

}  // namespace leonida
