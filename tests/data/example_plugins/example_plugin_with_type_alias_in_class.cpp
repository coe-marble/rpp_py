#include <rpp_plugin_types/rpp_common/MotionController2D.hpp>


std::map<std::string, std::string> COMPONENTS = {
    {"ctl1", "rpp_common::MotionController2D"},
};



class ComponentPluginWithTypeAliasInClass : public rpp_common::MotionController2D
{

    using Controller = rpp_common::MotionController2D;

public:
    ComponentPluginWithTypeAliasInClass() = default;

    virtual ~ComponentPluginWithTypeAliasInClass() = default;

    Controller::VectorPlanar step(Controller::Pose2D state, double dt) override
    {
        auto a = 5;
    }

};


