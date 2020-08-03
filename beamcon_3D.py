#!/usr/bin/env python
from numpy.testing._private.utils import verbose
from beamcon_2D import my_ceil, round_up
from logging import disable
from operator import countOf
from spectral_cube.utils import SpectralCubeWarning
import warnings
from astropy.utils.exceptions import AstropyWarning
import os
import stat
import sys
import numpy as np
import scipy.signal
from astropy import units as u
from astropy.io import fits, ascii
from astropy.table import Table
from spectral_cube import SpectralCube
from radio_beam import Beam, Beams
from radio_beam.utils import BeamError
from tqdm import tqdm, trange
import au2
import functools
import psutil
from IPython import embed
from mpi4py import MPI
comm = MPI.COMM_WORLD
#n_cores = comm.Get_size()
print = functools.partial(print, f'[{comm.rank}]', flush=True)
#try:
#    print = functools.partial(
#        print, f'[{psutil.Process().cpu_num()}]', flush=True)
#except AttributeError:
#    print = functools.partial(print, flush=True)
warnings.filterwarnings(action='ignore', category=SpectralCubeWarning,
                        append=True)
warnings.simplefilter('ignore', category=AstropyWarning)

#############################################
#### ADAPTED FROM SCRIPT BY T. VERNSTROM ####
#############################################


class Error(OSError):
    pass


class SameFileError(Error):
    """Raised when source and destination are the same file."""


class SpecialFileError(OSError):
    """Raised when trying to do a kind of operation (e.g. copying) which is
    not supported on a special file (e.g. a named pipe)"""


class ExecError(OSError):
    """Raised when a command could not be executed"""


class ReadError(OSError):
    """Raised when an archive cannot be read"""


class RegistryError(Exception):
    """Raised when a registry operation with the archiving
    and unpacking registeries fails"""


def _samefile(src, dst):
    # Macintosh, Unix.
    if hasattr(os.path, 'samefile'):
        try:
            return os.path.samefile(src, dst)
        except OSError:
            return False


def copyfile(src, dst, *, follow_symlinks=True, verbose=True):
    """Copy data from src to dst.

    If follow_symlinks is not set and src is a symbolic link, a new
    symlink will be created instead of copying the file it points to.

    """
    if _samefile(src, dst):
        raise SameFileError("{!r} and {!r} are the same file".format(src, dst))

    for fn in [src, dst]:
        try:
            st = os.stat(fn)
        except OSError:
            # File most likely does not exist
            pass
        else:
            # XXX What about other special files? (sockets, devices...)
            if stat.S_ISFIFO(st.st_mode):
                raise SpecialFileError("`%s` is a named pipe" % fn)

    if not follow_symlinks and os.path.islink(src):
        os.symlink(os.readlink(src), dst)
    else:
        with open(src, 'rb') as fsrc:
            with open(dst, 'wb') as fdst:
                copyfileobj(fsrc, fdst, verbose=verbose)
    return dst


def copyfileobj(fsrc, fdst, length=16*1024, verbose=True):
    #copied = 0
    total = os.fstat(fsrc.fileno()).st_size
    with tqdm(
            total=total,
            disable=(not verbose),
            unit_scale=True,
            desc='Copying file'
    ) as pbar:
        while True:
            buf = fsrc.read(length)
            if not buf:
                break
            fdst.write(buf)
            copied = len(buf)
            pbar.update(copied)


def getbeams(beamlog, verbose=False):
    """

    colnames=['Channel', 'BMAJarcsec', 'BMINarcsec', 'BPAdeg']
    """
    # Get beamlog
    if verbose:
        print(f'Getting beams from {beamlog}')

    beams = Table.read(beamlog, format='ascii.commented_header')
    for col in beams.colnames:
        idx = col.find('[')
        if idx == -1:
            new_col = col
            unit = u.Unit('')
        else:
            new_col = col[:idx]
            unit = u.Unit(col[idx+1:-1])
        beams[col].unit = unit
        beams[col].name = new_col
    # Get cubestats
    statfile = beamlog.replace('beamlog.', 'cubeStats-')
    if verbose:
        print(f'Getting stats from {statfile}')

    stats = Table.read(statfile, format='ascii.commented_header')
    with open(statfile, 'r') as f:
        lines = f.readlines()
    units = lines[1].strip().split()
    units[0] = ''
    units = [u.Unit(unit) for unit in units]
    for col, unit in zip(stats.colnames, units):
        stats[col].unit = unit

    nchan = len(beams)

    return beams, stats, nchan


def getfacs(datadict, convbeams, verbose=False):
    """Get beam info
    """
    facs = []
    for conbm, oldbeam in zip(convbeams, datadict['beams']):
        fac, amp, outbmaj, outbmin, outbpa = au2.gauss_factor(
            [
                conbm.major.to(u.arcsec).value,
                conbm.minor.to(u.arcsec).value,
                conbm.pa.to(u.deg).value
            ],
            beamOrig=[
                oldbeam.major.to(u.arcsec).value,
                oldbeam.minor.to(u.arcsec).value,
                oldbeam.pa.to(u.deg).value
            ],
            dx1=datadict['dx'].to(u.arcsec).value,
            dy1=datadict['dy'].to(u.arcsec).value
        )
        facs.append(fac)
    facs = np.array(facs)
    return facs


def smooth(image, dy, conbeam, sfactor, verbose=False):
    """Do the smoothing
    """
    if np.isnan(conbeam):
        return image*np.nan
    if np.isnan(image).all():
        return image
    else:
        # using Beams package
        if verbose:
            print(f'Using convolving beam', conbeam)
        pix_scale = dy
        gauss_kern = conbeam.as_kernel(dy)

        conbm1 = gauss_kern.array/gauss_kern.array.max()
        newim = scipy.signal.convolve(
            image.astype('f8'), conbm1, mode='same')
    newim *= sfactor
    return newim


def cpu_to_use(max_cpu, count):
    """Find number of cpus to use.
    Find the right number of cpus to use when dividing up a task, such
    that there are no remainders.
    Args:
        max_cpu (int): Maximum number of cores to use for a process.
        count (float): Number of tasks.

    Returns:
        Maximum number of cores to be used that divides into the number
        of tasks (int).
    """
    factors = []
    for i in range(1, count + 1):
        if count % i == 0:
            factors.append(i)
    factors = np.array(factors)
    return max(factors[factors <= max_cpu])


def worker(idx, cubedict, start=0):
    cube = SpectralCube.read(cubedict["filename"])
    plane = cube.unmasked_data[start+idx].value
    newim = smooth(plane, cubedict['dy'], cubedict['convbeams']
                   [start+idx], cubedict['facs'][start+idx], verbose=False)
    return newim


def makedata(files, outdir):
    datadict = {}
    for i, (file, out) in enumerate(zip(files, outdir)):
        # Set up files
        datadict[f"cube_{i}"] = {}
        datadict[f"cube_{i}"]["filename"] = file
        datadict[f"cube_{i}"]["outdir"] = out
        # Get metadata
        header = fits.getheader(file)
        dxas = header['CDELT1']*-1*u.deg
        datadict[f"cube_{i}"]["dx"] = dxas
        dyas = header['CDELT2']*u.deg
        datadict[f"cube_{i}"]["dy"] = dyas
        # Get beam info
        dirname = os.path.dirname(file)
        basename = os.path.basename(file)
        if dirname == '':
            dirname = '.'
        beamlog = f"{dirname}/beamlog.{basename}".replace('.fits', '.txt')
        datadict[f"cube_{i}"]["beamlog"] = beamlog
        beam, stats, nchan = getbeams(beamlog, verbose=verbose)
        datadict[f"cube_{i}"]["beam"] = beam
        datadict[f"cube_{i}"]["stats"] = stats
        datadict[f"cube_{i}"]["nchan"] = nchan
    return datadict


def commonbeamer(datadict, nchans, args, mode='natural', verbose=True):
    ### Natural mode ###
    if mode == 'natural':
        big_beams = []
        for n in trange(
            nchans,
            desc='Constructing beams',
            disable=(not verbose)
        ):
            majors = []
            minors = []
            pas = []
            for key in datadict.keys():
                major = datadict[key]['beams'][n].major
                minor = datadict[key]['beams'][n].minor
                pa = datadict[key]['beams'][n].pa
                if datadict[key]['mask'][n]:
                    major *= np.nan
                    minor *= np.nan
                    pa *= np.nan
                majors.append(major.value)
                minors.append(minor.value)
                pas.append(pa.value)

            majors = np.array(majors)
            minors = np.array(minors)
            pas = np.array(pas)

            majors *= major.unit
            minors *= minor.unit
            pas *= pa.unit
            big_beams.append(Beams(major=majors, minor=minors, pa=pas))

        # Find common beams
        bmaj_common = []
        bmin_common = []
        bpa_common = []
        for beams in tqdm(
            big_beams,
            desc='Finding common beam per channel',
            disable=(not verbose),
            total=nchans
        ):
            try:
                commonbeam = beams[~np.isnan(beams)].common_beam(tolerance=args.tolerance,
                                                                 nsamps=args.nsamps,
                                                                 epsilon=args.epsilon)
            except BeamError:
                if verbose:
                    print("Couldn't find common beam with defaults")
                    print("Trying again with smaller tolerance")

                commonbeam = beams[~np.isnan(beams)].common_beam(tolerance=args.tolerance*0.1,
                                                                 nsamps=args.nsamps,
                                                                 epsilon=args.epsilon)
            # Round up values
            commonbeam = Beam(
                major=my_ceil(
                    commonbeam.major.to(u.arcsec).value, precision=1
                )*u.arcsec,
                minor=my_ceil(
                    commonbeam.minor.to(u.arcsec).value, precision=1
                )*u.arcsec,
                pa=round_up(commonbeam.pa.to(u.deg), decimals=2)
            )
            bmaj_common.append(commonbeam.major.value)
            bmin_common.append(commonbeam.minor.value)
            bpa_common.append(commonbeam.pa.value)

        bmaj_common *= commonbeam.major.unit
        bmin_common *= commonbeam.minor.unit
        bpa_common *= commonbeam.pa.unit

        # Make Beams object
        commonbeams = Beams(
            major=bmaj_common,
            minor=bmin_common,
            pa=bpa_common
        )

    elif mode == 'total':
        majors = []
        minors = []
        pas = []
        for key in datadict.keys():
            major = datadict[key]['beams'].major
            minor = datadict[key]['beams'].minor
            pa = datadict[key]['beams'].pa
            major[datadict[key]['mask']] *= np.nan
            minor[datadict[key]['mask']] *= np.nan
            pa[datadict[key]['mask']] *= np.nan
            majors.append(major.value)
            minors.append(minor.value)
            pas.append(pa.value)

        majors = np.array(majors).ravel()
        minors = np.array(minors).ravel()
        pas = np.array(pas).ravel()

        majors *= major.unit
        minors *= minor.unit
        pas *= pa.unit
        big_beams = Beams(major=majors, minor=minors, pa=pas)

        if verbose:
            print('Finding common beam across all channels')
            print('This may take some time...')

        try:
            commonbeam = big_beams[~np.isnan(big_beams)].common_beam(tolerance=args.tolerance,
                                                                     nsamps=args.nsamps,
                                                                     epsilon=args.epsilon)
        except BeamError:
            if verbose:
                print("Couldn't find common beam with defaults")
                print("Trying again with smaller tolerance")

            commonbeam = big_beams[~np.isnan(big_beams)].common_beam(tolerance=args.tolerance*0.1,
                                                                     nsamps=args.nsamps,
                                                                     epsilon=args.epsilon)
        # Round up values
        commonbeam = Beam(
            major=my_ceil(
                commonbeam.major.to(u.arcsec).value, precision=1
            )*u.arcsec,
            minor=my_ceil(
                commonbeam.minor.to(u.arcsec).value, precision=1
            )*u.arcsec,
            pa=round_up(commonbeam.pa.to(u.deg), decimals=2)
        )
        # Make Beams object
        commonbeams = Beams(
            major=[commonbeam.major] * nchans * commonbeam.major.unit,
            minor=[commonbeam.minor] * nchans * commonbeam.minor.unit,
            pa=[commonbeam.pa] * nchans * commonbeam.pa.unit
        )

    if verbose:
        print('Final beams are:')
        for i, commonbeam in enumerate(commonbeams):
            print(f'Channel {i}:', commonbeam)

    for key in tqdm(
        datadict.keys(),
        desc='Getting convolution data',
        disable=(not verbose)
    ):
        # Get convolving beams
        conv_bmaj = []
        conv_bmin = []
        conv_bpa = []
        oldbeams = datadict[key]['beams']
        masks = datadict[key]['mask']
        for commonbeam, oldbeam, mask in zip(commonbeams, oldbeams, masks):
            if mask:
                convbeam = Beam(
                    major=np.nan*u.deg,
                    minor=np.nan*u.deg,
                    pa=np.nan*u.deg
                )
            else:
                convbeam = commonbeam.deconvolve(oldbeam)
            conv_bmaj.append(convbeam.major.value)
            conv_bmin.append(convbeam.minor.value)
            conv_bpa.append(convbeam.pa.to(u.deg).value)

        conv_bmaj *= convbeam.major.unit
        conv_bmin *= convbeam.minor.unit
        conv_bpa *= u.deg

        # Construct beams object
        convbeams = Beams(
            major=conv_bmaj,
            minor=conv_bmin,
            pa=conv_bpa
        )

        # Get gaussian beam factors
        facs = getfacs(datadict[key], convbeams)
        datadict[key]['facs'] = facs

        # Setup conv beamlog
        datadict[key]['convbeams'] = convbeams
        commonbeam_log = datadict[key]['beamlog'].replace('beamlog.',
                                                          f'beamlogConvolve-{mode}.')
        datadict[key]['commonbeams'] = commonbeams
        datadict[key]['commonbeamlog'] = commonbeam_log

        commonbeam_tab = Table()
        # Save target
        commonbeam_tab.add_column(np.arange(nchans), name='Channel')
        commonbeam_tab.add_column(commonbeams.major, name='Target BMAJ')
        commonbeam_tab.add_column(commonbeams.minor, name='Target BMIN')
        commonbeam_tab.add_column(commonbeams.pa, name='Target BPA')
        # Save convolving beams
        commonbeam_tab.add_column(convbeams.major, name='Convolving BMAJ')
        commonbeam_tab.add_column(convbeams.minor, name='Convolving BMIN')
        commonbeam_tab.add_column(convbeams.pa, name='Convolving BPA')
        # Save facs
        commonbeam_tab.add_column(facs, name='Convolving factor')

        # Write to log file
        units = ''
        for col in commonbeam_tab.colnames:
            unit = commonbeam_tab[col].unit
            unit = str(unit)
            units += unit + ' '
        commonbeam_tab.meta['comments'] = [units]
        ascii.write(
            commonbeam_tab,
            output=commonbeam_log,
            format='commented_header',
            overwrite=True
        )
        if verbose:
            print(f'Convolving log written to {commonbeam_log}')

    return datadict


def masking(nchans, cutoff, datadict, verbose=True):
    for key in datadict.keys():
        mask = np.array([False]*nchans)
        datadict[key]['mask'] = mask
    if cutoff is not None:
        for key in datadict.keys():
            majors = datadict[key]['beams'].major
            cutmask = majors > cutoff
            datadict[key]['mask'] += cutmask

    # Check for pipeline masking
    nullbeam = Beam(major=0*u.deg, minor=0*u.deg, pa=0*u.deg)
    for key in datadict.keys():
        nullmask = datadict[key]['beams'] == nullbeam
        datadict[key]['mask'] += nullmask
    return datadict


def initfiles(datadict, nchans, mode, verbose=True):
    for key in tqdm(datadict.keys(), desc='Initialising cubes'):
        with fits.open(datadict[key]["filename"], memmap=True, mode='denywrite') as hdulist:
            primary_hdu = hdulist[0]
            data = primary_hdu.data
            header = primary_hdu.header

        # Header
        commonbeams = datadict[key]['commonbeams']
        header = commonbeams[0].attach_to_header(header)
        primary_hdu = fits.PrimaryHDU(data=data, header=header)
        if mode == 'natural':
            header['COMMENT'] = 'The PSF in each image plane varies.'
            header['COMMENT'] = 'Full beam information is stored in the second FITS extension.'
            beam_table = Table(
                data=[
                    commonbeams.major.to(u.arcsec),
                    commonbeams.minor.to(u.arcsec),
                    commonbeams.pa.to(u.deg)
                ],
                names=[
                    'BMAJ',
                    'BMIN',
                    'BPA'
                ]
            )
            
            tab_hdu = fits.table_to_hdu(beam_table)
            new_hdulist = fits.HDUList([primary_hdu, tab_hdu])

        elif mode == 'total':
            new_hdulist = fits.HDUList([primary_hdu])
        # Set up output file
        outname = f"sm-{mode}." + os.path.basename(datadict[key]["filename"])
        outdir = datadict[key]['outdir']
        outfile = f'{outdir}/{outname}'
        datadict[key]['outfile'] = outfile
        if verbose:
            print(f'Initialsing to {outfile}')

        new_hdulist.writeto(outfile, overwrite=True)
    
    return datadict

    '''
    if not args.mpi:
        n_cores = args.n_cores
    width_max = n_cores
    width = cpu_to_use(width_max, cube.shape[0])
    n_chunks = cube.shape[0]//width

    for i in trange(
            n_chunks, disable=(not verbose),
            desc='Smoothing in chunks'
    ):
        start = i*width
        stop = start+width

        func = functools.partial(
            worker, start=start, cubedict=cubedict)
        arr_out = list(pool.map(func, [idx for idx in range(width)]))
        arr_out = np.array(arr_out)

        with fits.open(outfile, mode='update', memmap=True) as outfh:
            outfh[0].data[start:stop, 0, :, :] = arr_out[:]
            outfh.flush()

    if verbose:
        print('Updating header...')
    with fits.open(outfile, mode='update', memmap=True) as outfh:
        outfh[0].header = new_beam.attach_to_header(outfh[0].header)
        outfh.flush()
    # print(arr_out)
    '''


def main(args, verbose=True):
    """main script
    """
    #comm = MPI.COMM_WORLD
    nPE = comm.Get_size()
    myPE = comm.Get_rank()

    if comm.Get_rank() == 0:
        print(f"Total number of MPI ranks = {nPE}")
        # Parse args
        if args.dryrun:
            if verbose:
                print('Doing a dry run -- no files will be saved')

        # Check mode
        mode = args.mode
        if verbose:
            print(f"Mode is {mode}")
        if mode == 'natural' and mode == 'total':
            raise Exception("'mode' must be 'natural' or 'total'")
        if mode == 'natural':
            if verbose:
                print('Smoothing each channel to a common resolution')
        if mode == 'total':
            if verbose:
                print('Smoothing all channels to a common resolution')

        # Check cutoff
        cutoff = args.cutoff
        if args.cutoff is not None:
            cutoff = args.cutoff * u.arcsec
            if verbose:
                print('Cutoff is:', cutoff)

        # Check target
        bmaj = args.bmaj
        bmin = args.bmin
        bpa = args.bpa

        nonetest = [test is None for test in [bmaj, bmin, bpa]]

        if not all(nonetest) and mode is not 'total':
            raise Exception("Only specify a target beam in 'total' mode")

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

        files = sorted(args.infile)
        if files == []:
            raise Exception('No files found!')
        
        outdir = args.outdir
        if outdir is not None:
            if outdir[-1] == '/':
                outdir = outdir[:-1]
            outdir = [outdir] * len(files)
        else:
            outdir = [os.path.dirname(f) for f in files]

        datadict = makedata(files, outdir)

        # Sanity check channel counts
        nchans = np.array([datadict[key]['nchan'] for key in datadict.keys()])
        check = all(nchans == nchans[0])

        if not check:
            raise Exception('Unequal number of spectral channels!')

        else:
            nchans = nchans[0]

        # Construct Beams objects
        for key in datadict.keys():
            beam = datadict[key]['beam']
            bmaj = np.array(beam['BMAJ'])*beam['BMAJ'].unit
            bmin = np.array(beam['BMIN'])*beam['BMIN'].unit
            bpa = np.array(beam['BPA'])*beam['BPA'].unit
            beams = Beams(
                major=bmaj,
                minor=bmin,
                pa=bpa
            )
            datadict[key]['beams'] = beams

        # Apply some masking
        datadict = masking(
            nchans,
            cutoff,
            datadict,
            verbose=verbose
        )

        datadict = commonbeamer(
            datadict,
            nchans,
            args,
            mode=mode,
            verbose=verbose
        )

        if not args.dryrun:
            datadict = initfiles(datadict, nchans, mode, verbose=verbose)
            
            inputs = []
            for key in datadict.keys():
                for chan in range(nchans):
                    inputs.append( (key,chan) )
        

    else:
        if not args.dryrun:
            files = None
            datadict = None
            nchans = None
            inputs = None
    
    comm.Barrier()
    if not args.dryrun:
        files = comm.bcast(files, root=0)
        datadict = comm.bcast(datadict, root=0)
        nchans = comm.bcast(nchans, root=0)
        inputs = comm.bcast(inputs, root=0)

        dims = len(files) * nchans
        assert len(inputs) == dims
        count = dims // nPE
        rem = dims % nPE
        if myPE < rem:
            # The first 'remainder' ranks get 'count + 1' tasks each
            my_start = myPE * (count + 1)
            my_end = my_start + count

        else:
            #The remaining 'size - remainder' ranks get 'count' task each
            my_start = myPE * count + rem
            my_end = my_start + (count - 1)

        print(f"My start is {my_start}")
        print(f"My end is {my_end}")
        print(f"There are {nchans} channels, across {len(files)} files")
        for inp in inputs[my_start:my_end+1]:
            key, chan = inp
            print(key, chan)
            newim = worker(chan, datadict[key])
            outfile = datadict[key]['outfile']
            with fits.open(outfile, mode='update', memmap=True) as outfh:
                outfh[0].data[chan, 0, :, :] = newim
                outfh.flush()
            print(f"{outfile}  - channel {chan} - Done")
    else:
        if verbose:
            print('Done!')


def cli():
    """Command-line interface
    """
    import argparse

    # Help string to be shown using the -h option
    descStr = """
    Smooth a field of 3D cubes to a common resolution.

    Names of output files are 'infile'.sm.fits

    """

    # Parse the command line options
    parser = argparse.ArgumentParser(description=descStr,
                                     formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument(
        'infile',
        metavar='infile',
        type=str,
        help="""Input FITS image(s) to smooth (can be a wildcard) 
        - beam info must be in header.
        """,
        nargs='+')

    parser.add_argument(
        '--mode',
        dest='mode',
        type=str,
        default='natural',
        help="""Common resolution mode [natural]. 
        natural  -- allow frequency variation.
        total -- smooth all plans to a common resolution.
        """
    )

    parser.add_argument(
        "-v",
        "--verbose",
        dest="verbose",
        action="store_true",
        help="verbose output [False]."
    )

    parser.add_argument(
        "-d",
        "--dryrun",
        dest="dryrun",
        action="store_true",
        help="Compute common beam and stop [False]."
    )

    parser.add_argument(
        '-o',
        '--outdir',
        dest='outdir',
        type=str,
        default=None,
        help='Output directory of smoothed FITS image(s) [None - same as input].'
    )

    parser.add_argument(
        "--bmaj",
        dest="bmaj",
        type=float,
        default=None,
        help="BMAJ to convolve to [max BMAJ from given image(s)]."
    )

    parser.add_argument(
        "--bmin",
        dest="bmin",
        type=float,
        default=None,
        help="BMIN to convolve to [max BMAJ from given image(s)]."
    )

    parser.add_argument(
        "--bpa",
        dest="bpa",
        type=float,
        default=None,
        help="BPA to convolve to [0]."
    )

    parser.add_argument(
        '-m',
        '--mask',
        dest='masklist',
        type=str,
        default=None,
        help='List of channels to be masked [None]'
    )

    parser.add_argument(
        '-c',
        '--cutoff',
        dest='cutoff',
        type=float,
        default=None,
        help='Cutoff BMAJ value (arcsec) -- Blank channels with BMAJ larger than this [None -- no limit]'
    )

    parser.add_argument(
        "-t",
        "--tolerance",
        dest="tolerance",
        type=float,
        default=0.0001,
        help="tolerance for radio_beam.commonbeam."
    )

    parser.add_argument(
        "-e",
        "--epsilon",
        dest="epsilon",
        type=float,
        default=0.0005,
        help="epsilon for radio_beam.commonbeam."
    )

    parser.add_argument(
        "-n",
        "--nsamps",
        dest="nsamps",
        type=int,
        default=200,
        help="nsamps for radio_beam.commonbeam."
    )

    args = parser.parse_args()

    verbose = args.verbose

    main(args, verbose=verbose)


if __name__ == "__main__":
    cli()
