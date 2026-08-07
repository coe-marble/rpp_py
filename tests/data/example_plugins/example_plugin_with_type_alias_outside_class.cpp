#include <rpp_plugin_types/rpp_testing/MotionController2D.hpp>


std::map<std::string, std::string> COMPONENTS = {
    {"ctl1", "rpp_testing::MotionController2D"},
};


using Controller = rpp_testing::MotionController2D;

class ComponentPluginWithTypeAliasOutsideClass : public Controller
{


public:
    ComponentPluginWithTypeAliasOutsideClass() = default;

    virtual ~ComponentPluginWithTypeAliasOutsideClass() = default;

    Controller::VectorPlanar step(Controller::Pose2D state, double dt) override
    {
        auto a = 5;
    }

};


