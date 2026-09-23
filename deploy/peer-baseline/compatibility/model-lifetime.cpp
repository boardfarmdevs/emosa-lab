/* SPDX-License-Identifier: BSD-2-Clause-Patent
 * Probe real BPL ownership across shared-library/application destruction.
 * No radio, model connection or controller is started by this component test.
 */
#include <ambiorix_dummy.h>
#include <bpl/bpl_amx.h>
#include <bpl/bpl_cfg.h>
#include <cstdio>
#include <memory>
#include <string>

struct Runtime {
    ~Runtime() { std::puts("runtime_destroyed"); }
};
// Matches the application-global lifetime guarantee in both native mains.
static std::shared_ptr<Runtime> guarantee;

struct Model : beerocks::nbapi::AmbiorixDummy {
    ~Model() override { std::puts("model_destroyed"); }
};

int main(int argc, char **argv)
{
    guarantee = std::make_shared<Runtime>();
    beerocks::bpl::set_ambiorix_impl_ptr(std::make_shared<Model>());
    if (argc == 2 && std::string(argv[1]) == "--clear") {
        beerocks::bpl::set_ambiorix_impl_ptr(nullptr);
    }
    std::puts("main_returning");
    return 0;
}
