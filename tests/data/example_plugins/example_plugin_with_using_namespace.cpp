#include <rpp_plugin_types/rpp_common/MotionController2D.hpp>


std::map<std::string, std::string> COMPONENTS = {
    {"ctl1", "rpp_common::MotionController2D"},
};

using namespace rpp_common;

class ComponentPluginWithUsingNamespace : public MotionController2D
{

public:
    ComponentPluginWithUsingNamespace() = default;

    virtual ~ComponentPluginWithUsingNamespace() = default;

    MotionController2D::VectorPlanar step(MotionController2D::Pose2D state, double dt) override
    {
        auto a = 5;
    }

};


