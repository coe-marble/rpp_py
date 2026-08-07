#include <rpp_plugin_types/rpp_testing/MotionController2D.hpp>


std::map<std::string, std::string> COMPONENTS = {
    {"ctl1", "rpp_testing::MotionController2D"},
};

class ComponentPlugin : public rpp_testing::MotionController2D
{

public:
    ComponentPlugin() = default;

    virtual ~ComponentPlugin() = default;

    rpp_testing::MotionController2D::VectorPlanar step(rpp_testing::MotionController2D::Pose2D state, double dt) override
    {
        auto a = 5;
    }

};


