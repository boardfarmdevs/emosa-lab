// SPDX-License-Identifier: BSD-2-Clause-Patent
// Inject real libnl attribute messages into the actual rebuilt upstream parser.
// No kernel radio or EMOSA codec is used by this regression executable.
#include "nl80211_client_impl.h"
#include <linux/nl80211.h>
#include <netlink/genl/genl.h>
#include <iostream>
#include <vector>

struct Reply {
    int band;
    bool vht;
    int width; // HE PHY bits 3/4; this regression covers upstream cases 0, 1, 3.
    int iftypes;
};

class FixtureSocket : public bwl::nl80211_socket {
    std::vector<Reply> replies;
public:
    explicit FixtureSocket(std::vector<Reply> input) : replies(std::move(input)) {}
    bool send_receive_msg(int command, int, std::function<bool(nl_msg *)> create,
                          std::function<void(nl_msg *)> handle) override
    {
        if (command != NL80211_CMD_GET_WIPHY) return false;
        auto request = nlmsg_alloc();
        bool ok = create(request);
        nlmsg_free(request);
        if (!ok) return false;
        for (const auto &reply : replies) {
            auto msg = nlmsg_alloc();
            genlmsg_put(msg, 0, 0, 0, 0, 0, NL80211_CMD_NEW_WIPHY, 0);
            auto bands = nla_nest_start(msg, NL80211_ATTR_WIPHY_BANDS);
            auto band = nla_nest_start(msg, reply.band);
            if (reply.vht) nla_put_u32(msg, NL80211_BAND_ATTR_VHT_CAPA, 0);
            auto iftypes = nla_nest_start(msg, NL80211_BAND_ATTR_IFTYPE_DATA);
            for (int i = 0; i < reply.iftypes; ++i) {
                auto type = nla_nest_start(msg, i + 1);
                unsigned char phy[11] = {};
                phy[0] = reply.width << 3;
                nla_put(msg, NL80211_BAND_IFTYPE_ATTR_HE_CAP_PHY, sizeof(phy), phy);
                nla_nest_end(msg, type);
            }
            nla_nest_end(msg, iftypes);
            nla_nest_end(msg, band);
            nla_nest_end(msg, bands);
            handle(msg);
            nlmsg_free(msg);
        }
        return true;
    }
};

bool check(const char *name, std::vector<Reply> replies, std::vector<int> expected)
{
    bwl::nl80211_client_impl parser(std::unique_ptr<bwl::nl80211_socket>(
        new FixtureSocket(std::move(replies))));
    bwl::nl80211_client::radio_info info;
    bool ok = parser.get_radio_info("lo", info) && info.bands.size() == expected.size();
    std::cout << name << " observed=";
    for (size_t i = 0; i < info.bands.size(); ++i) {
        int length = (info.bands[i].wifi6_capability >> 40) & 15;
        std::cout << (i ? "," : "") << length;
        if (i >= expected.size() || length != expected[i]) ok = false;
    }
    std::cout << " result=" << (ok ? "PASS" : "FAIL") << std::endl;
    return ok;
}

int main()
{
    int failed = 0;
    failed += !check("he_without_vht", {{1, false, 0, 1}}, {4});
    failed += !check("he_with_vht", {{1, true, 0, 1}}, {4});
    failed += !check("he_160_without_vht", {{1, false, 1, 1}}, {8});
    failed += !check("he_160_with_vht", {{1, true, 1, 1}}, {8});
    failed += !check("he_both_wide_without_vht", {{1, false, 3, 1}}, {12});
    failed += !check("he_both_wide_with_vht", {{1, true, 3, 1}}, {12});
    failed += !check("two_iftypes", {{1, true, 1, 2}}, {8});
    failed += !check("split_repeated_band", {{1, true, 0, 1}, {1, true, 0, 1}}, {4});
    failed += !check("independent_bands", {{1, true, 3, 1}, {2, true, 0, 1}}, {12, 4});
    return failed ? 1 : 0;
}
