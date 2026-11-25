#include "../src/address_binding_registry.h"

#include <cstdio>
#include <thread>
#include <vector>

using Kind = leonida::AddressLookup::Kind;

static int failures = 0;
#define CHECK(c) do { if (!(c)) { std::printf("FAIL line %d: %s\n", __LINE__, #c); ++failures; } } while (0)

int main() {
    leonida::AddressBindingRegistry r;
    CHECK(r.lookup(0x1000).kind == Kind::none);
    r.bind(10, 0xA, 0x1000, 0x100);  // image
    r.bind(9, 0xB, 0x2000, 0x100);   // buffer
    auto hit = r.lookup(0x1010);
    CHECK(hit.kind == Kind::inside && hit.binding.handle == 0xA && hit.offset == 0x10);
    CHECK(r.lookup(0x1000).kind == Kind::inside);
    CHECK(r.lookup(0x10ff).kind == Kind::inside);
    hit = r.lookup(0x1100);  // one past the end
    CHECK(hit.kind == Kind::below && hit.binding.handle == 0xA && hit.distance == 0);
    hit = r.lookup(0x1180);
    CHECK(hit.kind == Kind::below && hit.distance == 0x80);
    hit = r.lookup(0x1f00);
    CHECK(hit.kind == Kind::above && hit.binding.handle == 0xB && hit.distance == 0x100);
    hit = r.lookup(0x500);
    CHECK(hit.kind == Kind::above && hit.binding.handle == 0xA && hit.distance == 0xb00);
    hit = r.lookup(0x9000);
    CHECK(hit.kind == Kind::below && hit.binding.handle == 0xB);

    // unbind: unknown, wrong size, wrong handle, then the real one
    CHECK(!r.unbind(0xA, 0x5000, 0x100));
    CHECK(!r.unbind(0xA, 0x1000, 0x80));
    CHECK(!r.unbind(0xC, 0x1000, 0x100));
    CHECK(r.size() == 2);
    CHECK(r.unbind(0xA, 0x1000, 0x100));
    CHECK(r.lookup(0x1010).kind != Kind::inside);
    CHECK(!r.unbind(0xA, 0x1000, 0x100));
    CHECK(r.unbind(0, 0x2000, 0x100));  // handle 0 matches any
    CHECK(r.size() == 0);

    // overlapping rebinds: the same base for two objects, then a rebind of one of them
    r.bind(10, 0x1, 0x4000, 0x100);
    r.bind(9, 0x2, 0x4000, 0x100);
    CHECK(r.size() == 2);
    r.bind(10, 0x1, 0x4000, 0x200);  // same object, same base: replaced
    CHECK(r.size() == 2);
    CHECK(r.lookup(0x4180).binding.handle == 0x1);
    CHECK(r.unbind(0x2, 0x4000, 0x100));
    CHECK(r.lookup(0x4010).binding.handle == 0x1);
    CHECK(!r.unbind(0x1, 0x4000, 0x100));  // the old size is no longer live
    CHECK(r.unbind(0x1, 0x4000, 0x200));
    CHECK(r.size() == 0);

    // text
    r.bind(10, 0xdead, 0x7000, 0x1000);
    CHECK(leonida::describe_address(r.lookup(0x7010)) == "inside Image 0xdead [0x7000, 0x8000) +0x10");
    CHECK(leonida::describe_address(r.lookup(0x8400)) ==
          "nearest below Image 0xdead [0x7000, 0x8000) distance 0x400");
    CHECK(leonida::describe_address(r.lookup(0x6000)).rfind("nearest above Image", 0) == 0);
    CHECK(leonida::describe_address({}) == "no binding known");

    // concurrent bind/unbind/lookup
    leonida::AddressBindingRegistry c;
    std::vector<std::thread> threads;
    for (std::uint64_t t = 0; t < 4; ++t)
        threads.emplace_back([&c, t] {
            for (std::uint64_t i = 0; i < 2000; ++i) {
                const std::uint64_t base = (t << 32) + i * 0x100;
                c.bind(9, t * 100000 + i + 1, base, 0x100);
                (void)c.lookup(base + 4);
                if (i % 2) c.unbind(t * 100000 + i + 1, base, 0x100);
            }
        });
    for (auto& th : threads) th.join();
    CHECK(c.size() == 4000);
    CHECK(c.binds() == 8000 && c.unbinds() == 4000);

    if (failures) std::printf("address_binding_registry: %d FAILED\n", failures);
    else std::printf("address_binding_registry: all passed\n");
    return failures ? 1 : 0;
}
