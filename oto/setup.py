from setuptools import setup

setup(
    name="oto",
    version="0.1.0",
    packages=["oto"],
    package_dir={"oto": "oto"},
    description="OTO API client",
    entry_points={
        "saleor.plugins": ["oto = oto.plugin:OTOPlugin"],
    },
)
