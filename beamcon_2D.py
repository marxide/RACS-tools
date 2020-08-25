#!/usr/bin/env python
import os
import sys
import numpy as np
import scipy.signal
from astropy import units as u
from astropy.io import fits
from radio_beam import Beam, Beams
from radio_beam.utils import BeamError
import au2
import functools
import schwimmbad
import psutil
from tqdm import tqdm
from IPython import embed
import matplotlib.pyplot as plt
import warnings
try:
    print = functools.partial(
        print, f'[{psutil.Process().cpu_num()}]', flush=True)
except AttributeError:
    print = functools.partial(print, flush=True)

#############################################
#### ADAPTED FROM SCRIPT BY T. VERNSTROM ####
#############################################


def round_up(n, decimals=0):
    multiplier = 10 ** decimals
    return np.ceil(n * multiplier) / multiplier


def my_ceil(a, precision=0):
    return np.round(a + 0.5 * 10**(-precision), precision)


def getbeam(datadict, new_beam, cutoff=None, verbose=False):
    """Get beam info
    """
    if verbose:
        print(f"Current beam is", datadict['oldbeam'])

    if cutoff is not None and datadict['oldbeam'].major.to(u.arcsec) > cutoff*u.arcsec:
        return np.nan, np.nan

    else:
        conbm = new_beam.deconvolve(datadict['oldbeam'])
        fac, amp, outbmaj, outbmin, outbpa = au2.gauss_factor(
            [
                conbm.major.to(u.arcsec).value,
                conbm.minor.to(u.arcsec).value,
                conbm.pa.to(u.deg).value
            ],
            beamOrig=[
                datadict['oldbeam'].major.to(u.arcsec).value,
                datadict['oldbeam'].minor.to(u.arcsec).value,
                datadict['oldbeam'].pa.to(u.deg).value
            ],
            dx1=datadict['dx'].to(u.arcsec).value,
            dy1=datadict['dy'].to(u.arcsec).value
        )

        return conbm, fac


def getimdata(cubenm, verbose=False):
    """Get fits image data
    """
    if verbose:
        print(f'Getting image data from {cubenm}')
    with fits.open(cubenm, memmap=True, mode='denywrite') as hdu:

        dxas = hdu[0].header['CDELT1']*-1*u.deg
        dyas = hdu[0].header['CDELT2']*u.deg

        nx, ny = hdu[0].data[0, 0, :,
                             :].shape[0], hdu[0].data[0, 0, :, :].shape[1]

        old_beam = Beam.from_fits_header(
            hdu[0].header
        )

        datadict = {
            'image': hdu[0].data[0, 0, :, :],
            'header': hdu[0].header,
            'oldbeam': old_beam,
            'nx': nx,
            'ny': ny,
            'dx': dxas,
            'dy': dyas
        }
    return datadict


def smooth(datadict, verbose=False):
    """Do the smoothing
    """
    if np.isnan(datadict["sfactor"]):
        if verbose:
            print('Beam larger than cutoff -- blanking')
        newim = np.ones_like(datadict['image']) * np.nan
        return newim
    else:
        # using Beams package
        if verbose:
            print(f'Smoothing so beam is', datadict["final_beam"])
            print(f'Using convolving beam', datadict["conbeam"])
        pix_scale = datadict['dy']

        gauss_kern = datadict["conbeam"].as_kernel(pix_scale)
        conbm1 = gauss_kern.array/gauss_kern.array.max()
        newim = scipy.signal.convolve(
            datadict['image'].astype('f8'), conbm1, mode='same')

        newim *= datadict["sfactor"]
        return newim


def savefile(datadict, filename, outdir='.', verbose=False):
    """Save file to disk
    """
    outfile = f'{outdir}/{filename}'
    if verbose:
        print(f'Saving to {outfile}')
    header = datadict['header']
    beam = datadict['final_beam']
    header = beam.attach_to_header(header)
    fits.writeto(outfile, datadict['newimage'], header=header, overwrite=True)


def worker(args):
    file, outdir, new_beam, clargs, verbose = args
    if verbose:
        print(f'Working on {file}')

    if outdir is None:
        outdir = os.path.dirname(file)

    if outdir == '':
        outdir = '.'

    outfile = os.path.basename(file)
    outfile = outfile.replace('.fits', f'.{clargs.suffix}.fits')
    if clargs.prefix is not None:
        outfile = clargs.prefix + outfile
    datadict = getimdata(file)

    conbeam, sfactor = getbeam(
        datadict,
        new_beam,
        cutoff=clargs.cutoff,
        verbose=verbose
    )

    datadict.update(
        {
            "conbeam": conbeam,
            "final_beam": new_beam,
            "sfactor": sfactor
        }
    )

    newim = smooth(datadict, verbose=verbose)
    datadict.update(
        {
            "newimage": newim,
        }
    )

    savefile(datadict, outfile, outdir, verbose=verbose)


def getmaxbeam(files, target_beam=None, cutoff=None, tolerance=0.0001, nsamps=200, epsilon=0.0005, verbose=False):
    """Get smallest common beam
    """
    beams = []
    for file in files:
        header = fits.getheader(file, memmap=True)
        beam = Beam.from_fits_header(header)
        beams.append(beam)

    beams = Beams(
        [beam.major.value for beam in beams]*u.deg,
        [beam.minor.value for beam in beams]*u.deg,
        [beam.pa.value for beam in beams]*u.deg
    )
    if cutoff is not None:
        flags = beams.major > cutoff*u.arcsec
    else:
        flags = np.array([False for beam in beams])
    try:
        cmn_beam = beams[~flags].common_beam(
            tolerance=tolerance, epsilon=epsilon, nsamps=nsamps)
    except BeamError:
        if verbose:
            print("Couldn't find common beam with defaults")
            print("Trying again with smaller tolerance")
        cmn_beam = beams[~flags].common_beam(
            tolerance=tolerance*0.1, epsilon=epsilon, nsamps=nsamps)

    # Round up values
    cmn_beam = Beam(
        major=my_ceil(cmn_beam.major.to(u.arcsec).value, precision=1)*u.arcsec,
        minor=my_ceil(cmn_beam.minor.to(u.arcsec).value, precision=1)*u.arcsec,
        pa=round_up(cmn_beam.pa.to(u.deg), decimals=2)
    )

    target_header = header
    dx = target_header['CDELT1']*-1*u.deg
    dy = target_header['CDELT2']*u.deg
    grid = dy
    conbeams = [cmn_beam.deconvolve(beam) for beam in beams]

    # Check that convolving beam will be nyquist sampled
    min_samps = []
    for b_idx, conbeam in enumerate(conbeams):
        # Get maj, min, pa
        samp = conbeam.minor / grid.to(u.arcsec)
        if samp < 2:
            min_samps.append([samp, b_idx])

    if len(min_samps) > 0:
        worst_idx = np.argmin([samp[0] for samp in min_samps], axis=0)
        samp_cor_fac, idx = 2 / \
            min_samps[worst_idx][0], int(
                min_samps[worst_idx][1])
        samp_cor_fac = my_ceil(samp_cor_fac.value, precision=1)
        conbeam = conbeams[idx]
        major = conbeam.major
        minor = conbeam.minor*samp_cor_fac
        pa = conbeam.pa
        # Check for small major!
        if major < minor:
            major = minor
            pa = 0*u.deg

        cor_beam = Beam(major, minor, pa)
        if verbose:
            print('Smallest common beam is:', cmn_beam)
        nyq_beam = beams[idx].convolve(cor_beam)
        nyq_beam = Beam(
            major=my_ceil(nyq_beam.major.to(u.arcsec).value, precision=1)*u.arcsec,
            minor=my_ceil(nyq_beam.minor.to(u.arcsec).value, precision=1)*u.arcsec,
            pa=round_up(nyq_beam.pa.to(u.deg), decimals=2)
        )
        embed()
        if verbose:
            print('Smallest common Nyquist sampled beam is:', nyq_beam)
        if target_beam is not None:
            if target_beam < nyq_beam:
                warnings.warn('TARGET BEAM WILL BE UNDERSAMPLED!')
                raise Exception("CAN'T UNDERSAMPLE BEAM - EXITING")
        if cmn_beam < nyq_beam:
                warnings.warn('COMMON BEAM WILL BE UNDERSAMPLED!')
                warnings.warn('SETTING COMMON BEAM TO NYQUIST BEAM')
                cmn_beam = nyq_beam

    else:
        nyq_beam = None

    return cmn_beam, beams


def main(pool, args, verbose=False):
    """Main script
    """
    if args.dryrun:
        if verbose:
            print('Doing a dry run -- no files will be saved')
    # Fix up outdir
    outdir = args.outdir
    if outdir is not None:
        if outdir[-1] == '/':
            outdir = outdir[:-1]
    else:
        outdir = None

    # Get file list
    files = sorted(args.infile)
    if files == []:
        raise Exception('No files found!')

    # Parse args
    bmaj = args.bmaj
    bmin = args.bmin
    bpa = args.bpa

    nonetest = [test is None for test in [bmaj, bmin, bpa]]

    if all(nonetest):
        target_beam = None

    elif not all(nonetest) and any(nonetest):
        raise Exception('Please specify all target beam params!')

    elif not all(nonetest) and not any(nonetest):
        target_beam = Beam(
            bmaj * u.arcsec,
            bmin * u.arcsec,
            bpa * u.deg
        )
        if verbose:
            print('Target beam is ', target_beam)

    # Find smallest common beam
    big_beam, allbeams = getmaxbeam(files,
                                    target_beam=target_beam,
                                    cutoff=args.cutoff,
                                    tolerance=args.tolerance,
                                    nsamps=args.nsamps,
                                    epsilon=args.epsilon,
                                    verbose=verbose)

    if target_beam is not None:
        if verbose:
            print('Checking that target beam will deconvolve...')

        mask_count = 0
        failed = []
        for i, (beam, file) in enumerate(
            tqdm(
                zip(allbeams, files),
                total=len(allbeams),
                desc='Deconvolving',
                disable=(not verbose)
            )
        ):
            try:
                target_beam.deconvolve(beam)
            except ValueError:
                mask_count += 1
                failed.append(file)
        if mask_count > 0:
            if verbose:
                print('The following images could not reach target resolution:')
                print(failed)
            raise Exception("Please choose a larger target beam!")

        else:
            new_beam = target_beam

    else:
        new_beam = big_beam

    if verbose:
        print(f'Final beam is', new_beam)
    inputs = [[file, outdir, new_beam, args, verbose]
              for i, file in enumerate(files)]

    if not args.dryrun:
        output = list(pool.map(worker, inputs))

    if verbose:
        print('Done!')


def cli():
    """Command-line interface
    """
    import argparse

    # Help string to be shown using the -h option
    descStr = """
    Smooth a field of 2D images to a common resolution.

    Names of output files are 'infile'.sm.fits

    """

    # Parse the command line options
    parser = argparse.ArgumentParser(description=descStr,
                                     formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument(
        'infile',
        metavar='infile',
        type=str,
        help='Input FITS image(s) to smooth (can be a wildcard) - beam info must be in header.',
        nargs='+')

    parser.add_argument(
        '-p',
        '--prefix',
        dest='prefix',
        type=str,
        default=None,
        help='Add prefix to output filenames.')

    parser.add_argument(
        '-s',
        '--suffix',
        dest='suffix',
        type=str,
        default='sm',
        help='Add suffix to output filenames [...sm.fits].')

    parser.add_argument(
        '-o',
        '--outdir',
        dest='outdir',
        type=str,
        default=None,
        help='Output directory of smoothed FITS image(s) [same as input file].')

    parser.add_argument("-v", "--verbose", dest="verbose", action="store_true",
                        help="verbose output [False].")

    parser.add_argument("-d", "--dryrun", dest="dryrun", action="store_true",
                        help="Compute common beam and stop [False].")

    parser.add_argument(
        "--bmaj",
        dest="bmaj",
        type=float,
        default=None,
        help="Target BMAJ (arcsec) to convolve to [None].")

    parser.add_argument(
        "--bmin",
        dest="bmin",
        type=float,
        default=None,
        help="Target BMIN (arcsec) to convolve to [None].")

    parser.add_argument(
        "--bpa",
        dest="bpa",
        type=float,
        default=None,
        help="Target BPA (deg) to convolve to [None].")

    parser.add_argument(
        '-c',
        '--cutoff',
        dest='cutoff',
        type=float,
        default=None,
        help='Cutoff BMAJ value (arcsec) -- Blank channels with BMAJ larger than this [None -- no limit]')

    parser.add_argument(
        "-t",
        "--tolerance",
        dest="tolerance",
        type=float,
        default=0.0001,
        help="tolerance for radio_beam.commonbeam.")

    parser.add_argument(
        "-e",
        "--epsilon",
        dest="epsilon",
        type=float,
        default=0.0005,
        help="epsilon for radio_beam.commonbeam.")

    parser.add_argument(
        "-n",
        "--nsamps",
        dest="nsamps",
        type=int,
        default=200,
        help="nsamps for radio_beam.commonbeam.")

    group = parser.add_mutually_exclusive_group()

    group.add_argument("--ncores", dest="n_cores", default=1,
                       type=int, help="Number of processes (uses multiprocessing).")
    group.add_argument("--mpi", dest="mpi", default=False,
                       action="store_true", help="Run with MPI.")

    args = parser.parse_args()

    pool = schwimmbad.choose_pool(mpi=args.mpi, processes=args.n_cores)
    if args.mpi:
        if not pool.is_master():
            pool.wait()
            sys.exit(0)

    verbose = args.verbose

    main(pool, args, verbose=verbose)
    pool.close()


if __name__ == "__main__":
    cli()
