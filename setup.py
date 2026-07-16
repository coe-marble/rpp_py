from setuptools import find_packages, setup


setup(
    name="rpp-py",
    version="0.1.0",
    description="Python implementation of RPP plugin system",
    packages=find_packages(include=["rpp_py", "rpp_py.*"]),
    package_dir={"": "."},
    include_package_data=True,
    entry_points={
        'console_scripts': [
            'rpp_component_server_python = rpp_py.cli.component_server:main'
        ]
    },
    install_requires=[],
)
